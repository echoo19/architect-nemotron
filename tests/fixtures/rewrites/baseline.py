async def run(context, task):
    await context.call("first")
    await context.call("second")
    return await context.call("final")
