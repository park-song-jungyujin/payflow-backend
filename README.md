# payflow-backend

## Quick Start (Hosted Demo)

1. Open the deployed web app: https://payflow-web-1095757595735.asia-northeast3.run.app/
2. Sign in with Google using the credentials provided in the testing instructions submitted with this project.
3. When prompted for an organization name, enter **"payflow"**.
4. Click **"Add to Slack"** and authorize it for your Slack workspace.
5. Payflow will send an onboarding DM asking for a PayPal payout email — reply with the PayPal sandbox email from the testing instructions to receive settlement payouts.
6. Upload the provided sample receipt image to any channel where the app is installed.
7. Continue from step 2 of **Reproduce the Demo** below.

## 1. Environment Setup

1. Clone all four repos side by side (`payflow-backend`, `payflow-agent`, `payflow-frontend`, `payflow-docs` as a submodule of each).
2. Prerequisites: Python 3.12, Node.js 20+, `uv`, and the `gcloud` CLI.
3. GCP project: enable Vertex AI, Firestore, Cloud Tasks, and Cloud Storage APIs. Run `gcloud auth application-default login` so `payflow-agent` and Gemini/Gemma calls can use ADC.
4. Create a PayPal **sandbox** app (Client ID/Secret) and a Slack app (bot token, signing secret, OAuth client ID/secret) pointed at a test workspace + channel.
5. Copy `.env.example` → `.env` in `payflow-backend` and fill in: `GCP_PROJECT`, `FIRESTORE_DATABASE`, `GEMINI_MODEL_ID`, `GEMMA_MODEL_ID`, `VERTEX_LOCATION`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_APP_CLIENT_ID`/`SECRET`, `PAYPAL_CLIENT_ID`/`SECRET`, `PAYPAL_ENV=sandbox`, `AGENT_SERVICE_URL`, `OIDC_AUDIENCE`.
6. Do the same in `payflow-agent`: `GCP_PROJECT`, `FIRESTORE_DATABASE`, `AGENT_MODEL`, `API_BASE_URL` (→ backend), `API_OIDC_AUDIENCE`, `OIDC_AUDIENCE`.
7. In `payflow-frontend`, `.env.local` just needs `API_BASE_URL` (→ backend) and `NEXT_PUBLIC_GOOGLE_CLIENT_ID`.

## 2. How to Run

1. Backend: `cd payflow-backend && uv sync && uv run uvicorn src.main:app --reload --port 8080`
2. Agent: `cd payflow-agent && uv sync && uv run uvicorn main:app --reload --port 8081` (point its `API_BASE_URL` at `:8080`)
3. Frontend: `cd payflow-frontend && npm install && npm run dev` (serves on `:3000`)
4. Note: production auth between services is OIDC (Cloud Tasks → agent, agent → backend). For local end-to-end testing this either needs a way to stand in for that, or the demo is run against the deployed Cloud Run URLs instead of localhost.

## 3. Reproduce the Demo

1. Upload the provided sample receipt image to the designated Slack channel.
2. Verify the backend parses it (Gemini structured output) and the claimant agent classifies it (business/personal) and writes a review draft.
3. If the claimant agent requests a re-upload (e.g. missing amount/date), respond in the Slack DM thread and confirm it re-parses.
4. On the web dashboard, create a settlement run from the confirmed claim(s).
5. Verify the executor agent's anomaly analysis appears on the run page (e.g. a flagged duplicate or personal-use item).
6. Approve the run from the web dashboard's approval card.
7. Verify the PayPal sandbox payout completes and the run status moves to `SETTLED`.
8. Run reconciliation and verify the generated XLSX report.
