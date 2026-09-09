import typer

from workflow.state import get_previous_task, get_current_task, set_current_task


app = typer.Typer(help="Switch to previous task")


@app.command()
def switch():
    """Switch to previous task"""
    prev_task = get_previous_task()
    if not prev_task:
        typer.echo("No previous task found.")
        return
    
    current_task = get_current_task()
    if current_task:
        typer.echo(f"Currently working on {current_task.key}. Switching to {prev_task.key}.")
        
        set_current_task(prev_task)
        typer.echo(f"✅ Switched to task {prev_task.key}")
