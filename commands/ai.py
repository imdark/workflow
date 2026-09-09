import typer

from workflow.ai import launch_ai_session, get_ai_provider
from workflow.state import get_current_task
from workflow.config import load_config, save_config


app = typer.Typer(help="AI assistant commands")


@app.command()
def ai():
    """Launch AI assistant session with RAG context"""
    try:
        current_task = get_current_task()
        provider = get_ai_provider()
        typer.echo(f"🤖 Using AI provider: {provider.name}")
        launch_ai_session(current_task)
    except Exception as e:
        typer.echo(f"❌ Error in AI session: {e}")
        import traceback
        traceback.print_exc()


@app.command("provider")
def ai_set_provider(provider: str = typer.Argument(..., help="AI provider name (claude or opencode)")):
    """Set the AI provider to use"""
    if provider not in ["claude", "opencode"]:
        typer.echo("❌ Error: Invalid provider. Supported providers: claude, opencode")
        return
    
    cfg = load_config()
    if "ai" not in cfg:
        cfg["ai"] = {}
    cfg["ai"]["provider"] = provider
    save_config(cfg)
    typer.echo(f"✅ AI provider set to: {provider}")
