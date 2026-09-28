# DRILL — SSSF-25 resolveReviewThread permission experiment

Throwaway. Never merges. Three review threads on this file (exp-A, exp-B, exp-C) are resolved
by three jobs whose GITHUB_TOKEN carries different permissions, to find which permission
`resolveReviewThread` needs.

line 7: exp-A target (pull-requests: write, contents: read — what the gate has today)
line 8: exp-B target (pull-requests: write, contents: write)
line 9: exp-C target (pull-requests: read, contents: write)
