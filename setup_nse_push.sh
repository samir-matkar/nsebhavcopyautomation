#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/samir-matkar/nsebhavcopyautomation.git"
BRANCH_NAME="nse-bhavcopy-automation"
BASE_BRANCH="${BASE_BRANCH:-main}"

# Prereq checks
command -v git >/dev/null 2>&1 || { echo "git not found. Install git and retry."; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "gh (GitHub CLI) not found. Install it and run 'gh auth login' then retry."; exit 1; }

WORKDIR="$(pwd)/nsebhavcopyautomation-temp-$$"
if [ -d "$WORKDIR" ]; then
  rm -rf "$WORKDIR"
fi

echo "Cloning ${REPO_URL} into ${WORKDIR}..."
git clone "$REPO_URL" "$WORKDIR"
cd "$WORKDIR"

echo "Creating branch ${BRANCH_NAME} from ${BASE_BRANCH} (make sure this base branch exists)..."
git fetch origin
git checkout -b "$BRANCH_NAME" "origin/${BASE_BRANCH}" || git checkout -b "$BRANCH_NAME"

echo "Adding files already present in this repository branch; ensure files are correct."

git add .
git commit -m "Add NSE bhavcopy automation (main.py, workflow, Apps Script, README)" || true
git push -u origin "$BRANCH_NAME"

echo "Creating PR..."
PR_URL=$(gh pr create --title "Add NSE bhavcopy automation" --body "Adds daily bhavcopy downloader and breakout detector. See README for setup instructions." --base "$BASE_BRANCH" --head "$BRANCH_NAME" --fill || true)

if [ -z "$PR_URL" ]; then
  echo "PR creation via gh failed or returned empty. You can create a PR manually from branch ${BRANCH_NAME} to ${BASE_BRANCH}."
else
  echo "PR created: $PR_URL"
fi

echo ""
echo "Next steps (manual):"
echo "1) In GitHub repo settings -> Secrets -> Actions add two secrets:"
echo "   - SHEET_ID : your Google Sheet ID"
echo "   - GSPREAD_SERVICE_ACCOUNT_JSON : full JSON content of the service account key"
echo "2) Create or share a Google Sheet with the service account email from key.json (grant Editor)."
echo "3) Run the workflow manually from Actions (Daily NSE250 Auto) to test."