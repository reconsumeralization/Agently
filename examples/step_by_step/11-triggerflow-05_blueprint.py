from agently import TriggerFlow, TriggerFlowRuntimeData


## TriggerFlow Blueprint: save / load
def triggerflow_blueprint():
    # Idea: export a flow blueprint and reuse it elsewhere.
    # Flow: build -> save_blueprint -> load_blueprint -> start
    # Expect: prints "AGENTLY".
    flow = TriggerFlow()

    async def upper(data: TriggerFlowRuntimeData):
        return str(data.value).upper()

    flow.to(upper).end()

    blueprint = flow.save_blueprint()

    # load the blueprint into a new flow
    flow_2 = TriggerFlow()
    flow_2.load_blueprint(blueprint)

    result = flow_2.start("agently")
    print(result)


# triggerflow_blueprint()
