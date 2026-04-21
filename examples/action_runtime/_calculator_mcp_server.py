import asyncio
import os

from fastmcp import FastMCP


app = FastMCP("calculator")


@app.tool
def add(first_number: float, second_number: float) -> float:
    """Add two numbers and round to two decimals."""
    return round(first_number + second_number, 2)


@app.tool
def multiply(first_number: float, second_number: float) -> float:
    """Multiply two numbers and round to four decimals."""
    return round(first_number * second_number, 4)


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    try:
        if transport == "http":
            port = int(os.getenv("MCP_PORT", "8080"))
            app.run(show_banner=False, transport="http", port=port)
        else:
            app.run(show_banner=False, transport="stdio")
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
