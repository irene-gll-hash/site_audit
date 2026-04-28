from mcp.server.fastmcp import FastMCP
import subprocess

mcp = FastMCP("site-audit")

@mcp.tool()
def audit_site(url: str, pages: int = 5) -> str:
    """
    Аудит сайта: запускает твой task.py
    """
    try:
        result = subprocess.run(
            [
                "/home/qifa/openclaw-env/bin/python",
                "/home/qifa/.openclaw/workspace/skills/site_text_audit/task.py",
                url,
                str(pages)
            ],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            return f"Ошибка выполнения:\n{result.stderr}"

        return f"Готово:\n{result.stdout}"

    except Exception as e:
        return f"Ошибка: {str(e)}"


if __name__ == "__main__":
    mcp.run()
