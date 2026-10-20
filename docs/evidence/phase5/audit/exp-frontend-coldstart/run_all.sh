#!/bin/bash
S=docs/evidence/phase5/audit/exp-frontend-coldstart
cd $S
FULL=2,5,15,30,60,75
run() { echo "##### $(date +%T) START $*"; node run_scenario.cjs "$@"; echo "##### $(date +%T) END $1"; }
run baseline_realcors --pages deep --checkpoints 2,5 --label baseline_realcors
run a_meta_delay60 --delay 60 --checkpoints $FULL
run a2_spinup60 --delay 60 --checkpoints $FULL
run b2_meta_delay60_503_once --delay 60 --checkpoints $FULL
run b_meta_delay60_503_always --delay 60 --checkpoints 2,5,15,30,60,75,120,150,185,200
run c_v2_same_shape --checkpoints $FULL
run c2_v2_changed_shape --checkpoints $FULL
run c3_v1_removed_404 --checkpoints $FULL
run d_503_firstN_plain --n 2 --checkpoints $FULL --label d_503_first2_plain
run d_503_firstN_plain --n 3 --checkpoints $FULL --label d_503_first3_plain
run d_503_firstN_json --n 3 --checkpoints $FULL --label d_503_first3_json
run d_503_for60s_plain --delay 60 --checkpoints $FULL
run e_502_html --checkpoints $FULL
run e_502_html_nocors --checkpoints $FULL
run e_504_html --checkpoints $FULL
run e_429_json --checkpoints $FULL
run e_1027_html --checkpoints $FULL
run e_429_html_nocors --checkpoints $FULL
for k in 502_html 502_html_nocors 504_html 429_json 1027_html 503_pooltimeout; do
  run f_analysis_$k --pages deep --checkpoints 2,5,15,30
done
echo "ALL DONE $(date +%T)"
