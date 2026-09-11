# 音频请求（4.1.4.8 开发版）

TTS/STT 使用独立的 `AudioModelRequest`，不走文本 `ModelRequest` 的 Prompt 链。
显式配置音频模型和连接；Agent 文本模型、历史、输出结构和 auto_continue 不会混入音频请求。

```python
import os
from agently import Agently, AudioInput, SpeechOptions

audio = Agently.create_audio_request(
    driver="OMLX",
    base_url=os.environ["AUDIO_BASE_URL"],
    api_key=os.getenv("AUDIO_API_KEY", ""),
    tts_model=os.environ["AUDIO_TTS_MODEL"],
    stt_model=os.environ["AUDIO_STT_MODEL"],
)
agent = Agently.create_agent()
agent.use_audio(audio)

# 在异步函数中：
speech = await agent.async_tts("欢迎参加会议。", voice=os.getenv("AUDIO_VOICE"))
transcript = await agent.async_stt(AudioInput(speech.data))
print(transcript.text)
```

同步对应方法为 `tts()` / `stt()`。独立对象也可直接调用，不依赖 Agent。
每次调用产生新请求，不是缓存读取。STT 接受文件路径或 `AudioInput`；非 WAV 数据应指定
真实 filename/content_type。只读取文件，不隐式录音、播放、下载 URL、转码或写文件。

`SpeechOptions` 明确定义格式、速度、语言、指令；`TranscriptionOptions` 定义语言和提示。
两者允许 extra 传递供应商参数，但不得覆盖 model/input/file/stream 等核心字段。
参数是否有效仍取决于驱动和模型。默认 120 秒是 HTTP 操作空闲超时，不是整个任务墙钟；
无自动重试、备用模型或音频补全行为。

## 持续消费与输出 auto break

基础 `tts/stt` 不变。以下四个方法可从独立 audio 对象或挂载后的 Agent 直接使用。
**auto break 区分输出形式，不区分输入是否已经分段。**

| 方法 | 输入 | 迭代输出 |
|---|---|---|
| stream_tts | str、非阻塞 Iterable[str] 或 AsyncIterable[str] | 连续的无文件头 PCM bytes |
| stream_tts_with_auto_break | 同上，共用内部文本组块 | 每段完整 SpeechResult 音频，可独立使用 |
| stream_stt | AsyncIterable[bytes]，显式 PCMFormat | 每个处理窗口的 TranscriptBlock |
| stream_stt_with_auto_break | 同上 | 识别后按文字句末交付 TranscriptSegment |

```python
from agently import PCMFormat, TextSegmentOptions, TranscriptionStreamOptions

# text_chunks 为调用方提供的文本碎片流；两个 TTS 模式共用此组块规则。
segments = TextSegmentOptions(expect_chars=300, tolerance_ratio=0.1, grace_chars=100)
async with agent.stream_tts(text_chunks, segments=segments) as stream:
    fmt = stream.audio_format  # 非空输入进入上下文时已就绪；空输入为 None
    async for pcm in stream:
        await pcm_sink.write(pcm)  # 使用 fmt 配置的应用消费端，不是 WAV 文件

async with agent.stream_tts_with_auto_break(fresh_text_chunks, segments=segments) as stream:
    async for speech in stream:
        await segment_sink.write(speech.data, speech.media_type)  # 每项一个完整音频段

# pcm_chunks 是显式格式的持续音频源。不要复用已耗尽的一次性 iterator。
async with agent.stream_stt_with_auto_break(
    pcm_chunks, audio_format=PCMFormat(sample_rate=16000),
    stream_options=TranscriptionStreamOptions(window_seconds=5, max_pending_chars=1000),
) as stream:
    async for segment in stream:
        print(segment.text, segment.reason, segment.first_block, segment.last_block)
```

TTS 在期望长度的比例区间内优先选换行/段落，其次句末，最后逗号，同级选最靠右边界。
无合适边界再读100字符宽限；仍有更早标点则使用它，完全无边界才硬切。
输入结束交付短尾，不把暂时无 token 当作结束。默认300字符只是可调工程值，不是模型最优长度。
任意输入分包不改变处理段；`segmenter: TextSegmenter` 可替换每流边界策略。
`max_input_chars` 默认65536，限制单个输入项；阻塞采集请自行适配为异步来源。

普通 TTS 当前只接收 PCM s16le WAV 或声明 `audio_format` 的原始PCM。
解析 WAV 容器后输出采样，不是去掉固定44字节；首段确定格式，后续不匹配即报错，
不隐式重采样。需要固定格式可传 `audio_format=PCMFormat(...)` 作校验。
`chunk_bytes` 默认8192，按完整采样帧输出。压缩格式请选择auto break，返回各自完整文件；
auto break暂不接受无自描述格式的裸PCM。连续PCM不能当作WAV文件，多个WAV也不能直接拼字节。
每段基础TTS完成后才交付该段，首段有等待时间；不保证韵律跨段一致或播放器永不卡顿。

STT 按采样帧聚合，默认每5秒调用一次基础识别，EOF处理剩余完整帧；半帧报错，不补零。
默认 `max_input_bytes=1048576` 限制单个输入包和处理窗口，`max_transcript_chars=65536`
限制单次转录；超限明确失败。普通块包含text/index/model/language和按采样计算的起止秒数。
`TranscriptResult.duration` 仍是提供方原始字段：oMLX当前返回识别处理耗时，不能当录音时长。

STT auto break消费**定稿转录**，跨块缓冲后按文字标点断句，不按VAD停顿、音频包或SSE事件断句。
`reason` 为 `sentence_end`、`limit` 或 `input_end`：无标点超过上限或EOF，交付原文余段，
不补造句号、不追加模型请求。来源块范围不是精准句子时间戳。中英文句末规则不是通用语义分句器；
跨ASCII字母/数字块边界补显示空格，不修复被识别窗口切开的单词。原始块保持不变。
模型自行添加的标点会影响分句，短窗识别也可能漏词/重复；框架不靠关键词去重修正文意。

四种流都必须 `async with`。按下游消费推进，不后台无限预取；每流最多一个模型请求。
提前关闭/取消/异常不合成未交付尾部，不自动重试已播放内容；已输出前缀不是完整成功。
只关闭本流资源，不接管共享麦克风。真实采集超速需由输入适配器报告溢出或显式应用策略，
背压不会让现实讲话暂停。框架分段缓冲有界，不代表第三方驱动整段响应分配也被框架限制。
输入流是单消费者、一次性的；无默认保存/重放/全双工/录音/播放。

不要把Agent的thinking、工具事件或可被validate/retry替换的临时文字自动播报。
需要最终结果保证时先取得最终文本；低延迟播报不可撤回文字须由调用方明确接受。

## 提供方原生输出流

`audio.supported_operations` 表示框架组合能力；`audio.driver.supported_operations` 表示原生驱动能力，
都不是服务健康证明。OpenAICompatible有基础tts/stt即可使用上述组合接口，不要求原生双向流。
OMLX额外支持原生WAV输出和完整文件上传后的transcript.text.delta/done SSE；
高级调用通过 `audio.driver.stream_tts(SpeechRequest(...))` /
`audio.driver.stream_stt(TranscriptionRequest(...))` 显式使用。

原生音频包不等于完整文件；原生STT的done是完整转录，替换而非追加delta；缺done报错。
原生持续输入 `driver.stream_stt_input(...)` 仍是自定义驱动接缝，内置尚未适配。
它与框架按窗口持续调用stt是两种交互机制，不因本轮组合支持而宣称已实现原生实时ASR。

## 插件替换与 Execution 依赖

驱动实现 `AudioModelRequester`，构造参数为 `AudioConnection`；通过
`Agently.plugin_manager.register("AudioModelRequester", Driver, activate=False)` 注册。
也可直接 `AudioModelRequest(driver_instance, tts_model=..., stt_model=...)`。
驱动可以替换整个传输机制，不限于 HTTP 参数。`use_audio` 还接受整个 `AudioCapability`
协议的替换实现。注册不等于 Agent 挂载；未挂载就调用会报错。
`use_audio(None)` 解除未来访问，不代应用关闭共享对象。

Execution 插件声明 `required_agent_capabilities = ("audio",)`，工厂在构造前检查依赖。
共享 Execution 实现在创建时绑定对象，生产方通过 `require_agent_capability("audio")`
取用。动态依赖也通过此方法在使用前绑定；已绑定的引用不随 Agent 后续替换而漂移。
子 Execution 声明自己的依赖，创建子执行不会授予权限。完整替换 Execution 的插件也必须
履行该合同。依赖存在不代表授权、模型支持、健康状态或已经实际调用。
带额外能力绑定的 save/load 当前明确不支持，不能序列化客户端或假称自动重绑与重放安全。

本切片没有增加文本模型 token 事件，也未把音频纳入 Execution 的文本请求预算。
应用需要对音频设置自己的并发准入和任务总时限；不宣称费用统一核算、取消回滚或持久恢复。

可运行示例：[四种持续输出](../../../examples/audio/continuous_audio.py)、
[基础与原生流回环](../../../examples/audio/tts_stt_roundtrip.py)。
