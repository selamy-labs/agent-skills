# System Context

This repository is a versioned public catalog of reusable agent skills. Its
repository-owned validators and GitHub Actions workflows keep published content
well-formed, tested, and suitable for public use.

```mermaid
C4Context
    title System Context for Agent Skills
    Person(contributor, "Contributor", "Authors and reviews repository changes")
    System(catalog, "Agent Skills", "Publishes versioned skills and manifests")
    System_Ext(actions, "GitHub Actions", "Validates and releases content")
    System_Ext(agent, "Compatible agent tooling", "Installs and uses versioned skills")

    Rel(contributor, catalog, "Proposes changes through pull requests")
    Rel(actions, catalog, "Validates and releases")
    Rel(agent, catalog, "Installs versioned skills from")
```

Update this diagram when the repository's publishing, validation, release, or
consumption boundaries change.
