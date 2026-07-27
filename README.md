# Linear dataset price estimation

A working pipeline that measures a company's Linear workspace and turns it into a priced dataset estimate, without a human ever reading the data and without an API key ever travelling by email. Built as a case study for Ooak Data.

## How it works

The founder receives a link and opens an intake page hosted on Render. They create a read-only Linear API key and paste it into the page. The Flask application uses that key to query the Linear GraphQL API, paginating through every ticket including archived ones, and computes aggregate metrics in memory. It then posts those aggregates to an n8n webhook, which appends a single row to a Google Sheet. The founder sees a confirmation and is told to revoke the key.

The API key lives in the Flask process memory for the duration of the scan and goes no further. It never reaches n8n, the Sheet, or any log. Only the seventeen aggregate metrics leave the application.

Pricing formulas sit in the Sheet rather than in the code, because the coefficients can change fast.

## What is measured

Volume comes first: teams, projects, tickets, comments. It is the weakest signal on its own.

Months active matters because an eighteen-month workspace beats the same volume crammed into two months. Long trajectories make realistic tasks.

Conversational density is captured by the share of tickets carrying at least one comment and the median number of comments per ticket. A ticket with no thread has no context to reconstruct, and small teams tend to talk in Slack rather than in Linear.

The share of resolved tickets matters because an agent must reproduce a sequence. Without state movement there is nothing to learn.

The share of tickets mentioning another tool is the differentiating metric. It directly measures how many cross-tool tasks are derivable from the workspace.

Text volume, in characters and estimated tokens, is the cost side of the equation: it approximates the anonymisation workload.

Golden tickets are the synthesis and the figure that actually gets priced. A ticket qualifies when it has a description longer than two hundred characters, at least three comments, at least two distinct participants, and a resolved status reached through at least two state transitions.

## Running it

Install dependencies with pip install -r requirements.txt.

To run the scan from the command line, set LINEAR_API_KEY in the environment and run python3 linear_estimate.py. It prints a report and writes linear_estimate.json.

To run the intake page locally, run python3 intake_app.py and open http://localhost:5000/scan/demo-token.

On Render the build command is pip install -r requirements.txt and the start command is gunicorn intake_app:app --timeout 300 --workers 1. The three hundred second timeout is not optional: gunicorn defaults to thirty seconds and would kill a scan mid-flight. The environment variable N8N_WEBHOOK_URL must point at the n8n webhook that appends to the Sheet.

Prospect links take the form /scan/<token>, with one token per company defined in VALID_TOKENS. Unknown tokens return a 404.

## Files

linear_estimate.py holds the Linear GraphQL scan and the metric computation, and is importable as a module.

intake_app.py holds the intake page, the key handling, and the post of aggregates to n8n.

requirements.txt lists flask, requests and gunicorn.
