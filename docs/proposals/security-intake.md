# Automating the security@meridianlabs.ai intake queue

**Status:** proposal for review (Eric, then Charles). This document rides in
this repo for review convenience only. It is a Meridian ops proposal, not part
of inspect_ai, and is not intended to merge.

**Audience:** Charles Teague (dragonstyle), sole admin of both Meridian's
Google Workspace and the GitHub org. Several setup steps can only be done by
Charles; those are collected in an explicit checklist below.

## Background

- Upstream `SECURITY.md` merged 2026-08-25 (PR UKGovernmentBEIS/inspect_ai#5036,
  authored by dragonstyle, merged by epatey). It publishes
  security@meridianlabs.ai as the private vulnerability reporting channel and
  commits to SLAs: acknowledge within 3 business days, triage decision within
  10, fix targeted within 30. No public issues or PRs for vulnerabilities;
  disclosure via GitHub security advisories.
- GitHub private vulnerability reporting (PVR) is currently disabled on both
  UKGovernmentBEIS/inspect_ai and meridianlabs-ai/inspect_ai (checked
  2026-08-25).
- security@meridianlabs.ai is a real Google Workspace mailbox, not a group or
  alias. Eric accesses it via Gmail delegation. External mail delivers
  (tested 2026-08-25). The domain's MX is Google Workspace.

The problem: a delegated mailbox is a silent corner. Nothing pings anyone when
a report arrives, no one owns a report by default, and there is no shared view
of what's pending against the published SLAs. This proposal turns each report
into a GitHub issue with an owner, a board position, and an alarm.

## Design overview

1. A Google Apps Script owned by the security@ account bridges mail into
   issues in a new private repo, `meridianlabs-ai/inspect-security`.
2. An org auto-add workflow puts every new issue on the Atlas project board in
   an intake status, unassigned. Unassigned-in-intake means "no one owns
   this". Claiming = self-assign; the item leaves intake.
3. Two notification layers: repo watching (native), plus an org-owned
   scheduled GitHub Action that alarms on unclaimed or un-acked items past
   SLA, and on a dead bridge via a heartbeat.

meridianlabs-ai/inspect_ai must stay public (PRs promote upstream from it),
hence the new private repo for intake content.

## Components

### 1. Mail to GitHub bridge (Apps Script)

A Google Apps Script owned by the security@ account itself, not any
individual's account, so it survives personnel changes. Installing it requires
Charles to sign in as security@; Gmail delegation is not sufficient for script
ownership.

- Trigger: time-driven, every 5–15 minutes.
- A new mail thread becomes a new issue in `meridianlabs-ai/inspect-security`:
  title = subject; body = sender, date, message text, attachments.
- A reply on a known Gmail thread becomes a comment on the mapped issue. The
  script persists a Gmail thread-id to issue mapping, e.g. a marker in the
  issue body or a label.
- The script source lives in a Meridian git repo and deploys via
  [clasp](https://github.com/google/clasp), so changes are reviewable and the
  deployed code isn't only in the Apps Script editor.
- GitHub auth: a fine-grained PAT (or a GitHub App installation token) scoped
  to only the `inspect-security` repo, stored in the script's Script
  Properties.
- No third-party services (Zapier, Make, etc.). They would read security
  mail.

### 2. Private repo: meridianlabs-ai/inspect-security

Holds one issue per report. Private preserves confidentiality; issues give
threading, comments, attachments, notifications, and an audit timeline.
Queue members get repo access and watch the repo.

### 3. Shared view: Atlas board

An org auto-add workflow adds every new inspect-security issue to the Atlas
project board in an intake status, unassigned. The board is the shared view;
intake + unassigned is the "nobody owns this yet" state. To claim a report,
self-assign the issue; the board workflow moves it out of intake.

### 4. Notification layers and SLA sweep

- Native: queue members watch the private repo; GitHub notifies on every new
  issue and comment.
- Backstop: an org-owned scheduled GitHub Action sweeps intake for items
  unclaimed or un-acknowledged past threshold and escalates (Slack message or
  issue comment; open decision below). Thresholds align with the published
  3-business-day acknowledge SLA. The alarm is org infrastructure. It must
  never live on anyone's laptop.

### 5. Bridge health (heartbeat)

Apps Script triggers fail silently. The script maintains a heartbeat, e.g.
updates a timestamp somewhere the sweep Action can read (an issue, or a file
in the private repo). The sweep alarms when the heartbeat goes stale, so a
dead bridge is as loud as an unclaimed report.

### 6. Personal funnels

Personal tooling (e.g. Eric's board-sync) consumes self-assigned issues
through existing issue search. These are consumers only; nothing
team-critical depends on them.

## Why this shape

- Private repo issues over Atlas draft items: drafts have no comments, no
  notifications, no attachments, and no audit timeline, and a
  convert-to-issue misclick could land a confidential report in a public
  repo. Issues restore threading and native notifications; the private repo
  preserves confidentiality; Atlas remains the shared board view.
- Why not rely on Gmail delegation alone: a delegated mailbox is a silent
  corner nobody gets pinged for. That is the status quo this proposal fixes.

## Steps only Charles can do

Charles is the only Workspace admin and the only GitHub org owner, so these
are his:

- [ ] Sign in **as** security@ (he has, or can reset, its password) and
      install the Apps Script and its time-driven trigger. Delegation is
      insufficient for script ownership.
- [ ] Create the private repo `meridianlabs-ai/inspect-security` (org owner).
- [ ] Mint the fine-grained PAT scoped to only that repo (or install a GitHub
      App); place it in the script's Script Properties.
- [ ] Configure the Atlas auto-add workflow for the new repo.
- [ ] Grant repo access to queue members.
- [ ] If Slack escalation is chosen: set up the escalation Action's Slack
      webhook/secret.

Everything else (writing the script, the clasp repo, the sweep Action, docs
for queue members) can be done by Eric or other queue members and handed to
Charles for the steps above.

## Open decisions (requested from Charles/team)

Listed as decisions, not answered here:

1. Queue membership: who watches the repo and can claim reports?
2. Rotation or default owner when nobody claims within threshold?
3. Exact alarm thresholds (the ack SLA is 3 business days; when does the
   sweep warn vs. escalate?).
4. Atlas status-option naming for the intake state.
5. Escalation channel: Slack message vs. issue comment.

## Deliberately deferred

- Responsible-disclosure policy details.
- Whether to also enable GitHub private vulnerability reporting (PVR)
  upstream. Recommended that Charles consider it as a complement (a second,
  GitHub-native channel for reporters), but it is out of scope here.
