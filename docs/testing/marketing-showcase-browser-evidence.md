# Marketing showcase browser evidence

Tested September 16, 2026 UTC in a disposable Jhin installation, using the seeded synthetic owner and a dedicated `ShowcaseQA Browser` team / `ShowcaseQA Writer` agent. These are browser and persistence checks, not a claim that Mindy generated or Ashley reviewed an article.

The browser used `http://127.0.0.1:4300`, proxied only to the isolated Docker test stack. The production browser session and production capture policies were not changed.

## Memory policy controls

| Check | Result and evidence |
| --- | --- |
| Team policy creation | Passed through the Settings form. Selected `ShowcaseQA Browser`, `Editorial style`, and only `ShowcaseQA Writer`. UI showed Active. API read-back confirmed exact team ID, actor ID, class, human grantor and prospective effective time. |
| Persistence after restart | Passed. The entire dedicated Docker daemon was restarted while correcting test infrastructure. The retained database and browser reload still showed the same Active policy. |
| Keyboard revocation | Passed. Enter on `Revoke policy` changed the persisted record to Revoked. |
| Company policy creation | Passed through the same form, using Company / Company facts / only the synthetic writer. API read-back confirmed workspace scope rather than team scope. |
| Company policy revocation | Passed by keyboard, followed by browser reload and API read-back. Both owned policies remained present and revoked. |
| Editorial lesson scope guard | Passed. Choosing Company plus Approved editorial lessons showed `Choose a team audience for editorial lessons`; Save remained disabled. No invalid policy was submitted. |
| Mobile layout | Passed at requested viewport 390 x 844. Screenshot inspection showed readable, stacked controls and policy cards. DOM measured page scroll width equal to client width (380 px with scrollbar), with no horizontal overflow. |

Safe local receipts are in `.tmp/marketing-ui-fixture.json` and `.tmp/marketing-ui-memory-receipts.json`. The team policy is `01a0a8d9-3763-71a0-a9df-c52aa9d22ee5`; the company policy is `01a0a8dc-dbb0-7522-b6f6-8b8e7e072c00`. Both are test-only records. No model task was assigned to the synthetic writer and no editorial memory was inferred from this UI exercise.

Actual semantic capture, source authority, private-source exclusion, revocation at execution time and retrieval isolation have separate backend evidence in the acceptance matrix. These UI checks do not replace those tests or the pending real-agent conversation rehearsal.

## Editorial preview

A disabled, credential-free `ShowcaseQA Preview` connection holds an explicitly synthetic pending review. Its article contains **65,456 HTML characters**, 96 numbered sections, an inline local illustration with alt text/credit, a closing sentinel, and a harmless script that would change a DOM marker if execution were allowed. Hidden paused placeholder agents represent the author and director; no model task or external provider request created this fixture.

| Check | Result and evidence |
| --- | --- |
| Full saved package | Passed. The actual API reports `complete=true`; the browser reports 65,456 article characters and displays the ending sentinel inside the review iframe. Content exceeds the former 60,000-character risk boundary. |
| Draft-only wording | Passed. Both mobile and desktop show `Draft only · publication disabled`, pending status, designated-director identity links, and the explicit synthetic provenance feedback. There is no human Publish or Approve control on this view. |
| Script isolation | Passed. The iframe has an empty sandbox attribute. The marker text remained `SANDBOX MARKER: scripts must remain blocked.` and `data-showcase-qa-executed` was absent on the iframe document after navigation and refresh. This establishes the specific script boundary, not an exhaustive browser-security audit. |
| Metadata inspection | Passed. Enter expands/collapses the image/author/tag/SEO disclosure. The saved metadata includes the inline illustration's alt text and credit. This is not a real Unsplash hotlink/rendering test. |
| Mobile preview | Passed at 390 x 844. Screenshot showed a readable dialog and accessible footer. Dialog width 358 px / content width 356 px, without content overflow; iframe client/scroll width both 281 px. |
| Desktop preview | Passed after resetting the temporary viewport. The ending sentinel remained present; iframe client/scroll width both 595 px. Screenshot showed the complete metadata and draft-only notice within the dialog. |
| Refresh | Passed using Refresh status and browser reload. The same saved revision reopened from the deep link with the full article and pending/draft-only state. |
| Keyboard dismissal/reopening | Passed. Escape closed the dialog; Enter on Inspect review reopened it; Escape restored focus to Inspect review. |

Review ID: `9fd4c21f-5083-5738-9aa0-03c7f710ebe4`; connection ID: `51ff33ba-1db2-5d06-abfa-1edf5f21b579`. The safe seed and receipt are `.tmp/marketing-ui-review-fixture.py` and `.tmp/marketing-ui-review-fixture.json`. The viewport override was reset after testing.

These checks establish full preview, layout, refresh and keyboard behavior on the actual web application. The real Mindy/Ashley article, actual selected Unsplash cover, factual quality, and live review wording remain separate pending acceptance work.
