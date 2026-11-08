"""`wf install` -- set this machine up: dependencies, launcher, completion, aliases, skills.

Registered on the main app in cli.py as a plain command (`wf install`)."""

import typer


def install(
    system: bool = typer.Option(False, "--system", help="Also install the claude alias machine wide (/etc/zshrc, /etc/bashrc; asks for sudo)"),
    deps: bool = typer.Option(True, "--deps/--no-deps", help="pip install requirements and the workflow package"),
    launcher: bool = typer.Option(True, "--launcher/--no-launcher", help="Symlink wf into /usr/local/bin"),
    completion: bool = typer.Option(True, "--completion/--no-completion", help="Shell completion for $SHELL"),
    aliases: bool = typer.Option(True, "--aliases/--no-aliases", help="alias claude='wf ai --here --' in ~/.zshrc and ~/.bashrc"),
    skills: bool = typer.Option(True, "--skills/--no-skills", help="Link the bundled Claude skills into ~/.claude/skills"),
    shell: str = typer.Option(None, "--shell", help="bash or zsh (default: $SHELL)"),
):
    """Install wf on this machine: dependencies, the wf launcher on PATH,
    shell completion, the `claude` alias and the bundled Claude skills.

    Idempotent: run it again after a pull. First-time setup (the venv
    itself) is install.sh, which ends by running this.
    """
    from workflow.install import install_all

    report = install_all(deps=deps, launcher=launcher, completion=completion,
                         aliases=aliases, skills=skills, system=system, shell=shell)
    for step in report.steps:
        icon = "✅" if step.ok and step.changed else ("✔ " if step.ok else "❌")
        typer.echo(f"{icon} {step.name}: {step.detail}")
    if report.failed:
        typer.echo(f"\n{len(report.failed)} step(s) need attention (see above)")
        raise typer.Exit(1)
    typer.echo("\n✅ wf is installed. Open a new shell for the alias and completion.")
