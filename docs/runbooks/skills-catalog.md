# Skills Catalog

Operators browse installed skills from Admin Console **System → Skills**. Skill
files stay in git. The catalog does not write `SKILL.md` or agent `skills:`
lists. Inbox still owns draft approve/reject. **Paste skill** and **Upload
folder** create drafts in the managed skills volume; see [`skills.md`](skills.md).

## Hashes

| Route | Page |
|---|---|
| `#/system/skills` | Catalog list |
| `#/system/skills/{id}` | Detail, doctor, used-by, Attach |

`{id}` is `decodeURIComponent` of the path (ids may contain slashes). Inbox
drafts are not catalog rows. Detail may link to `#/inbox/skill_draft/{id}` when
a draft with the same id exists.

Used-by is derived at read time from workflow stage `skill_hints` and cron
`skill_id` / `skill_hints`. `coding-backend/` and `drafts/` are hidden.

## Attach query params

The catalog does not PUT workflow or cron records. Attach only changes the hash.
The existing Workflows or Cron editor finishes Save / Activate.

| Target | Hash |
|---|---|
| Workflow | `#/workflows/{type}?attachSkill={id}&stage={name}` |
| Cron job | `#/system/cron?attachSkill={id}&job={name}` |
| New cron job | `#/system/cron?attachSkill={id}` |

The builder appends `{id}` to that stage's `skill_hints` once when the skill is
in `/admin/workflow-definitions/catalog` (approved/injectable). Otherwise it
shows a banner and does not add silently. Cron opens the named job (or the
create dialog) with `skill_id` set. An unknown job shows a banner.

The attach query is cleared from the hash after apply so refresh does not
duplicate the hint.
