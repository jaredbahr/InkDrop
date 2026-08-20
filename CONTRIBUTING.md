# Contributing to InkDrop

Thanks for taking the time. InkDrop is a closed alpha built by one person, and
outside eyes on it are genuinely useful — bug reports especially.

Before anything else, the one thing that surprises people:

## This repository is a mirror

InkDrop is developed in a separate private repository. What you see here is a
curated export of it, published on each release. Nothing is committed here
directly, and the next export overwrites whatever is in `main`.

That has one consequence worth knowing up front: **a pull request against this
repository cannot be merged as-is.** Not because it isn't wanted — because the
merge would be reverted by the next export. A change that lands has to land in
the development tree first, and then arrive here the same way everything else
does.

So what actually happens to a good PR is that the fix ships, and the diff
doesn't. That is not a brush-off, and it is not a rewrite for its own sake. It
is the only route the change has.

## What this means for you

You do not need to work around any of the above. Open the pull request or the
issue, describe what you found, and let it be reviewed. If it turns out to be
real, it gets fixed and you get the credit — see below.

## How a contribution is reviewed

Every PR and issue gets read properly and checked against the development
codebase, not against this mirror. That check answers three questions:

1. **Is it real?** The mirror can lag the development tree by a release or
   more, so something broken here is sometimes already fixed there. That is
   worth knowing either way, and it is not a wasted report — it confirms the
   fix and tells us the export is behind.
2. **Is the diagnosis complete?** Reports are often right about the symptom and
   land one layer above the cause. When that happens, the report is still what
   made the bug findable.
3. **Is it worth building?** Not everything that can be fixed should be, right
   now. A change gets weighed against what it costs to build, test, and carry
   from here on. Some good suggestions get declined on those grounds, and when
   they do, you'll get the actual reason rather than silence.

Nothing is auto-accepted and nothing is auto-rejected.

## Credit

If your report is what surfaced a bug, you are credited for it — in the commit
or PR that fixes it, and in the release notes — **whether or not the shipped
fix resembles your diff.** Finding the problem is the hard part and it is
credited as its own contribution. A fix that ended up in a different file, or
one layer deeper than the report, is still your find.

If wording you wrote is good, it tends to get used verbatim. That gets credited
too.

## What makes a report easy to act on

- **What you expected, what happened, and how to see it again.** A config
  snippet and the relevant log lines beat a description of both.
- **Your setup**, if it's relevant: the image tag, whether you run SLSKD or
  other clients in separate containers, and anything non-default about your
  mounts or paths.
- **One thing per report.** Two unrelated problems in one thread means the
  quieter one gets lost.

## If you do open a pull request

Keep it to the change itself. In particular:

- **No incidental reformatting.** A whitespace pass over files you didn't
  otherwise touch buries the actual change and makes the diff impossible to
  review. It also can't survive the export, since those files are generated
  upstream.
- **Leave CI and release workflows alone.** Publishing here is wired to the
  release process, and a second pipeline pushing images would collide with it.
- **Say what you verified.** "I set this env var and the path still didn't
  change" is worth more than a description of the code.

Small drive-by fixes are welcome inside a PR — just mention them in the
description so they don't get missed. It happens.

## Security

Please don't open a public issue for anything exploitable. Use GitHub's private
vulnerability reporting on this repository instead.

## License

InkDrop is GPL-3.0-or-later. Contributions are accepted under the same license.
