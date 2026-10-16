import argparse

from edgar_core.model_profiles import ModelProfileError, validate_provider_configuration


def main() -> None:
    """CLI entry: demo rules-only RAG (optional query from argv)."""
    parser = argparse.ArgumentParser(description="Search installed D&D rules references.")
    parser.add_argument("query", nargs="*", help="Rules question (default: ability check proficiency)")
    args = parser.parse_args()
    try:
        validate_provider_configuration()
    except ModelProfileError as exc:
        parser.exit(1, f"Configuration error: {exc}\n")
    query = " ".join(args.query) if args.query else "ability check proficiency"
    from rules_engine.rag import retrieve_rules_snippets

    snippets = retrieve_rules_snippets(query, limit_per_collection=3)
    print(f"Query: {query!r}\nFound {len(snippets)} snippet(s).")
    for i, s in enumerate(snippets[:10], 1):
        text = (s.get("text") or "")[:200]
        print(f"{i}. [{s.get('collection')}] {text}...")


if __name__ == "__main__":
    main()
