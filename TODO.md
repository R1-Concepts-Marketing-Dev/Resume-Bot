# TODO

## Pre-launch cleanup

- [ ] **Delete the offline test scorer once `GOOGLE_OAUTH_REFRESH_TOKEN` is set up.**
      The offline variant was added so we could iterate on Claude prompts and
      filter wording before the jobs@ OAuth Playground step was finished. Once
      the regular test scorer works (writes results to the "Test Results" tab
      in the Sheet), the offline one becomes redundant clutter.

      Files to remove:
      - `.github/workflows/test-scorer-offline.yml`
      - `src/test_scorer_offline.py`

      From then on, use the **Test scorer** workflow instead — it pulls
      filters live from the Sheet (so it reflects whatever HR has edited
      via the web UI, not the YAML seed), and stores results in a
      reviewable spreadsheet tab.

- [ ] Rename column F1 on the Candidates tab from "Best-Fit Role(s)" to
      "Best-Fit Roles & Scores" so the header matches what the bot writes.
      (One-time manual edit; bot won't overwrite existing headers.)

- [ ] Create `Pending Opportunities` Drive folder, share with jobs@ as
      Editor, add its ID as `DRIVE_FOLDER_PENDING` GitHub Secret.

- [ ] Add `GOOGLE_OAUTH_REFRESH_TOKEN` GitHub Secret once Phase C OAuth
      Playground step is done.

- [ ] Apply the `resume-bot/processed` Gmail label to existing historical
      messages in jobs@ that you DON'T want auto-replied to, before the
      first real run. (Or temporarily pause all 3 templates in the web UI
      for the first backlog-clearing run.)
