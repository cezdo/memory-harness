# Memory Harness

- Keep instance configuration, profiles, real conversations, runtime data and private Git history outside this repository.
- Maintain README.md in Chinese and README.en.md as its English mirror.
- Use synthetic fixtures and temporary instances. Run `python3 tools/test.py` after code changes.
- Preserve source attribution and revision history. New interpretations supersede earlier records.
- Update bundled prompts, patches and host resources when changing their callers.
- Keep host and service installation explicit; tests must not modify a running instance.
- Use “user” for roles in project design, interfaces, contributor rules and public introductions. Address a person as “you” in text written for them, using their preferred name when needed; the model uses “I”. Choose wording for the audience, purpose and narrative viewpoint, and preserve a document’s viewpoint when presenting it to a model. Read names from private configuration; preserve original names in source conversations and historical records.
- Preserve authors, addressees and readers as separate viewpoints. Shared expression rules live in `mh/express/`; source conversations and quoted text retain their words and speaker attribution. Delivery evidence must match the returned text.
