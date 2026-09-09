import typer

from workflow.rag import get_rag


app = typer.Typer(help="RAG context commands")


@app.command("regenerate-context")
def regenerate_context():
    """Regenerate project context file"""
    rag = get_rag()
    rag.rebuild_index()
    typer.echo("✅ Project context regenerated")


@app.command("rag-info")
def rag_info():
    """Show RAG information and available categories"""
    rag = get_rag()
    
    typer.echo(f"📊 RAG Information:")
    typer.echo(f"  Total chunks: {len(rag.chunks)}")
    typer.echo(f"  Available categories: {', '.join(rag.list_categories())}")
    typer.echo(f"  Index created: {rag.index.get('created', 'Unknown')}")
    typer.echo(f"  Context directory: {rag.context_dir}")


@app.command("search-context")
def search_context(query: str):
    """Search project context using RAG"""
    rag = get_rag()
    
    print(f"🔍 Searching for: {query}")
    results = rag.retrieve_context(query, max_chunks=10)
    
    if not results:
        typer.echo("No relevant context found.")
        return
    
    typer.echo(f"📋 Found {len(results)} relevant chunks:")
    for i, chunk in enumerate(results, 1):
        typer.echo(f"\n{i}. {chunk['title']} (Relevance: {chunk.get('similarity_score', 0):.3f})")
        typer.echo(f"   Category: {chunk['category']}")
        typer.echo(f"   Content: {chunk['content'][:200]}{'...' if len(chunk['content']) > 200 else ''}")


@app.command("list-categories")
def list_categories():
    """List all available context categories"""
    rag = get_rag()
    
    categories = rag.list_categories()
    typer.echo("📂 Available Context Categories:")
    
    for category in categories:
        chunks = rag.get_chunks_by_category(category)
        typer.echo(f"\n{category.upper()}:")
        typer.echo(f"  ({len(chunks)} chunks)")
        for chunk in chunks[:5]:
            typer.echo(f"    - {chunk['title']}")
