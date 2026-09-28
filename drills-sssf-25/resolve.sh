#!/usr/bin/env bash
# DRILL (SSSF-25): resolve the review thread whose first comment is $MARK; optionally reply first.
set -uo pipefail
id="$(gh api graphql -f query='query($n:Int!){ repository(owner:"BJGLLC",name:".github"){ pullRequest(number:$n){ reviewThreads(first:20){nodes{id comments(first:1){nodes{body}}}} } } }' -F n="$PR" \
      --jq ".data.repository.pullRequest.reviewThreads.nodes[] | select(.comments.nodes[0].body == \"$MARK\") | .id")"
echo "thread=$id"
[ -n "$id" ] || { echo "RESULT $MARK thread-not-found"; exit 2; }
if [ "${REPLY:-0}" = 1 ]; then
  if gh api graphql -f query='mutation($id:ID!,$b:String!){ addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$id, body:$b}){ comment{id} } }' -F id="$id" -F b="$MARK reply via GITHUB_TOKEN"; then
    echo "RESULT $MARK reply=ok"
  else
    echo "RESULT $MARK reply=FAILED"
  fi
fi
if gh api graphql -f query='mutation($id:ID!){ resolveReviewThread(input:{threadId:$id}){ thread{id isResolved} } }' -F id="$id"; then
  echo "RESULT $MARK resolve=ok"
else
  echo "RESULT $MARK resolve=FAILED"; exit 1
fi
