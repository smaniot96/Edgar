def main() -> None:
    """CLI entry: demo rules-only RAG (optional query from argv)."""
    import sys

    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "ability check proficiency"
    from rules_engine.rag import retrieve_rules_snippets

    snippets = retrieve_rules_snippets(query, limit_per_collection=3)
    print(f"Query: {query!r}\nFound {len(snippets)} snippet(s).")
    for i, s in enumerate(snippets[:10], 1):
        text = (s.get("text") or "")[:200]
        print(f"{i}. [{s.get('collection')}] {text}...")


if __name__ == "__main__":
    main()
