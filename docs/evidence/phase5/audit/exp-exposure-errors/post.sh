#!/bin/bash
# usage: post.sh <port> <name> <payloadfile> <endpoint> [extra curl args]
S=docs/evidence/phase5/audit/exp-exposure-errors
PORT=$1; NAME=$2; FILE=$3; EP=$4; shift 4
$S/probe.sh $PORT "post_${NAME}" -X POST -H 'Content-Type: application/json' --data-binary @$S/payloads/$FILE "$@" http://127.0.0.1:$PORT/api/v1/analysis/$EP
