# Résumé Analysis DAG

Exercises a multi-branch diamond DAG where `resume-scope-runner` fans out into parallel branches that converge at `finalize-resume-analysis`.

## Run Locally from Repository Root

```sh
rippflow dev examples/resume/resume.workflow.json \
  --handlers examples/resume/resume.handlers.json \
  --report resume-report.json
```

Expected: all eight phases complete and converge cleanly at `finalize-resume-analysis` (exit code `0`).
