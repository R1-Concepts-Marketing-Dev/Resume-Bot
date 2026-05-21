// Public configuration for the filter editor.
// Fill these in once before pushing to GitHub Pages.

window.RESUME_BOT_CONFIG = {
  // From GCP > APIs & Services > Credentials > OAuth 2.0 Client IDs (web app).
  oauthClientId: "1009035063205-9eksjahnrq8f95o85n6nf0dkfnji7gcm.apps.googleusercontent.com",

  // The Sheet that contains the Filters, Templates and Candidates tabs.
  sheetId: "1i2NdWrWM-1Yu9S8Pd2M7VNaKPEqoLvUcFUtXfcwR8gI",

  // Tab names inside that Sheet. Defaults match the bot's expectations.
  filtersTab: "Filters",
  templatesTab: "Templates",

  // Optional: restrict sign-in to a specific Workspace domain.
  hostedDomain: "r1concepts.com",

  // Optional: when set, the toolbar shows a "View resumes" button.
  resumeFolderUrl: "https://drive.google.com/drive/u/1/folders/1erAtOiN5URqrUO9xV4cPtylP29haOipK",
};
