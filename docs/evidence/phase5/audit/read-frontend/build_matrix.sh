#!/bin/zsh
S=docs/evidence/phase5/audit/read-frontend
cd $S/fe
run() { name=$1; shift; echo "=== build $name ($*)"; env "$@" AUDIT_CACHE_DIR=$S/vitecache ./node_modules/.bin/vite build --config vite.config.ts --outDir $S/builds/$name --emptyOutDir --logLevel warn 2>&1 | tail -5; echo "base-url-related strings in bundle:"; grep -o -E 'http://localhost:[0-9]+|https://api\.example\.invalid|/api/v1' $S/builds/$name/assets/*.js | sed 's#.*/assets/##' | sort | uniq -c; }
mv .env.local .env.local.bak
run A_unset -u VITE_API_BASE_URL
run B_empty VITE_API_BASE_URL=
run C_slash VITE_API_BASE_URL=/
run D_abs VITE_API_BASE_URL=https://api.example.invalid/
mv .env.local.bak .env.local
run E_laptop_envlocal -u VITE_API_BASE_URL
