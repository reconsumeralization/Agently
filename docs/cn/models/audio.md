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

## 分开看流式输入和流式输出

| 驱动 | 完整 TTS/STT | TTS 流式输出 | 上传完整音频后的 STT 流式输出 | 持续输入音频 |
|---|---|---|---|---|
| OpenAICompatible | 服务/模型支持对应端点时可用 | 未声明 | 未声明 | 未声明 |
| OMLX | 支持 | WAV 字节流 | transcript.text.delta/done SSE | 尚未适配 |
| 自定义驱动 | 按实现声明 | 按实现声明 | 按实现声明 | 可实现 stream_stt_input |

```python
async with agent.audio.stream_tts("欢迎。", options=SpeechOptions(response_format="wav")) as chunks:
    async for chunk in chunks:
        await audio_sink.write(chunk)  # 应用提供的消费端；每块不等于一个完整 WAV 文件。

async with agent.audio.stream_stt("recording.wav") as events:
    async for event in events:
        if event.kind == "delta":
            print(event.text, end="", flush=True)
        else:
            final_text = event.text  # 完整最终转录，不应再次追加到 delta 后。
```

必须使用 async with，提前 break、失败和取消都会释放连接。部分输出不是最终成功结果。
STT 未收到 done 就结束会报 `AudioProtocolError`；音频字节流正常结束只证明传输完成，
不证明发音、内容质量或模型没有提前停下。流消费只提供异步入口。

`stream_stt_input(chunks, audio_format=PCMFormat(...))` 是独立的持续输入接口。
内置驱动暂时明确拒绝，不会把输入全部缓冲后假称实时。oMLX 服务有 WebSocket 接口且部分
模型支持实时，并不等于框架已完成该适配；不能从模型安装状态推断可用性。

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

可运行示例：[TTS→STT 回环](../../../examples/audio/tts_stt_roundtrip.py)。
