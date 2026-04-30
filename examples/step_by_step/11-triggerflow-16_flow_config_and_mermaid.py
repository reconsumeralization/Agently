import asyncio
from pathlib import Path

from agently import TriggerFlow, TriggerFlowRuntimeData


ASSET_DIR = Path(__file__).with_name("11-triggerflow-16_assets")
JSON_PATH = ASSET_DIR / "review_flow.json"
YAML_PATH = ASSET_DIR / "review_flow.yaml"
MERMAID_PATH = ASSET_DIR / "review_flow.mmd"


async def normalize(data: TriggerFlowRuntimeData):
    return str(data.input).strip().lower()


async def store(data: TriggerFlowRuntimeData):
    await data.async_set_state("normalized", data.input)


def register_handlers(flow: TriggerFlow):
    flow.register_chunk_handler(normalize)
    flow.register_chunk_handler(store)
    return flow


def build_flow():
    flow = TriggerFlow(name="step-16-flow-config")
    register_handlers(flow)
    flow.to(normalize).to(store)
    return flow


def export_assets(flow: TriggerFlow):
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    flow.get_json_flow(JSON_PATH)
    flow.get_yaml_flow(YAML_PATH)
    MERMAID_PATH.write_text(flow.to_mermaid(mode="detailed"), encoding="utf-8")
    print("Exported:", JSON_PATH, YAML_PATH, MERMAID_PATH)


async def run_flow(flow: TriggerFlow, value: str):
    execution = flow.create_execution()
    await execution.async_start(value)
    return await execution.async_close()


async def triggerflow_flow_config_and_mermaid_demo():
    source_flow = build_flow()
    export_assets(source_flow)
    source_state = await run_flow(source_flow, "  Agently  ")

    json_flow = TriggerFlow()
    register_handlers(json_flow)
    json_flow.load_json_flow(JSON_PATH)
    json_state = await run_flow(json_flow, "  JSON  ")

    yaml_flow = TriggerFlow()
    register_handlers(yaml_flow)
    yaml_flow.load_yaml_flow(YAML_PATH)
    yaml_state = await run_flow(yaml_flow, "  YAML  ")

    assert source_state["normalized"] == "agently"
    assert json_state["normalized"] == "json"
    assert yaml_state["normalized"] == "yaml"
    print({"source": source_state, "json": json_state, "yaml": yaml_state})


if __name__ == "__main__":
    asyncio.run(triggerflow_flow_config_and_mermaid_demo())
