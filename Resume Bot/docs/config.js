// Public configuration for the filter editor.
// Neither of these values is sensitive:
//   - oauthClientId is meant to be public (it's in every Google sign-in button).
//   - sheetId is the same ID that appears in the Sheet's URL.
// Fill these in once before pushing to GitHub Pages.

window.RESUME_BOT_CONFIG = {
  // From GCP → APIs & Services → Credentials → OAuth 2.0 Client IDs (web app).
  // Authorized JavaScript origin must be your GitHub Pages URL,
  // e.g. https://r1concepts.github.io
  oauthClientId: "PASTE_OAUTH_CLIENT_ID_HERE.apps.googleusercontent.com",

  // The Sheet that contains the Filters tab and the Candidates dashboard tab.
  // Same ID the bot uses (SHEET_ID secret).
  sheetId: "PASTE_SHEET_ID_HERE",

  // Tab name inside that Sheet. Match this to FILTERS_TAB_NAME on the bot side.
  filtersTab: "Filters",

  // Optional: restrict sign-in to a specific Workspace domain.
  // Set to "" to allow any signed-in Google account.
  hostedDomain: "r1concepts.com",
};
