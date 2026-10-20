# jaystarz1/newsjack fork

Fork of elvisun/newsjack for Jay's marketing department. Changes from upstream:

- `skills/find-journalists/scripts/find_email.py`: free journalist email finder. Published
  address first (article, author page, bios), then common newsroom patterns checked against the
  outlet's mail server, then a labelled pattern guess (format learned from the outlet's pages or
  passed with `--pattern`). No paid service.
- `skills/find-journalists/SKILL.md`: Local Mode fills Contact from the finder, labelled by method.

Installed as the `newsjack` plugin marketplace in `~/marketing-department` only. Pull upstream with
`git fetch upstream && git merge upstream/main`.
