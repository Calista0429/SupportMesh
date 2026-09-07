# SupportMesh Skills

At startup SupportMesh reads Skills from `SUPPORTMESH_SKILLS_DIR` and injects the matching ones into the system prompt of the corresponding agent. Skills are the right place for business procedures, service scripts, troubleshooting SOPs, billing review boundaries, escalation rules and prohibitions.

Three Skills ship by default:

```text
skills/general_customer_service/SKILL.md  # general: reception, clarification, triage, complaints, human handoff
skills/technical_support/SKILL.md         # technical: troubleshooting, API errors, deployment config, security limits
skills/billing_support/SKILL.md           # billing: charges, refunds, invoices, subscriptions, finance review
```

## Skill file format

Give each Skill its own directory with the main file named `SKILL.md`:

```text
skills/<skill_name>/SKILL.md
```

Start the file with simple front matter:

```markdown
---
name: Technical support standards
description: Troubleshooting and escalation standards for the TechnicalAgent
keywords: error,exception,api,deployment,timeout,500,401,logs
agents: technical
enabled: true
---
```

Fields:

- `name` -- the display name, which appears in the prompt given to the model.
- `description` -- a short summary, useful when debugging through the `/skills` endpoint.
- `keywords` -- trigger words. The Skill is injected only when the user's message matches one; matching is word-boundary aware, so `bill` will not fire on `billing`. Separate them with commas.
- `agents` -- which agents this applies to: `general`, `technical`, `billing`. Comma-separated.
- `enabled` -- `true` or `false`.

## Writing guidelines

- Put the important rules in the first half of the document; anything past the prompt budget is truncated.
- One Skill describes one responsibility. Do not mix technical, billing and general rules in one file.
- Keep the stable sections: role, process, escalation conditions and prohibitions.
- For privacy, payments, passwords, verification codes, API keys and tokens, state explicitly that they must not be collected or disclosed.
- Use conservative wording for anything you cannot guarantee: "usually", "expected", "subject to verification".
- Spell out the escalation conditions for anything needing a human, finance or second-line engineering.

## Hot reload

After editing a Skill file, no restart is needed:

```bash
curl -X POST http://localhost:8000/skills/reload
```

Inspect what loaded, along with any parse errors:

```bash
curl http://localhost:8000/skills
```
