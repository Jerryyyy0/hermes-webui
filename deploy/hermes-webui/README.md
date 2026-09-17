# Hermes WebUI Helm Chart

`.gitlab-ci.yml` packages this chart as a Harbor OCI artifact after it has pushed
the matching ARM64 runtime image. The packaged chart has an immutable `appVersion` and
`image.repository` for the commit that produced it; the source-chart repository
field is intentionally empty so no registry hostname is committed to Git.

## CI trigger behavior

- A change on `dev` builds and pushes the immutable base and runtime image tags for
  that commit, then publishes its OCI Chart. An Argo CD Application that tracks the
  CI prerelease versions automatically syncs the resulting application deployment.
- The default branch and release tags build the images and publish the OCI Chart.
  Only those Chart versions are available for Argo CD to reconcile.
- If the Harbor credentials are protected GitLab variables, protect the `dev`
  branch as well; otherwise its image job cannot receive the credentials and
  fails before the build starts.

## Required GitLab CI/CD variables

- `HARBOR_HOST` — defaults to the verified 137 Harbor address
  `192.168.1.137:18000`; override only when the registry moves.
- `HARBOR_PROJECT` — Harbor project name.
- `HARBOR_USER` and `HARBOR_PASS` — robot account credentials. Mark the token
  masked, and protect it for the production branch and release tags.
- `CI_DOCKER_CLI_IMAGE` and `CI_DOCKER_DIND_IMAGE` — ARM64 CI tool images
  mirrored to a Harbor project the Runner can pull before the job starts. The
  Docker CLI image must include `git`. `CI_HELM_IMAGE` already defaults to the verified public 137 image
  `192.168.1.137:18000/helm/helm:v4.2.4`.
- `UV_IMAGE`, `OPENSANDBOX_IMAGE`, `BROWSER_IMAGE` — ARM64 build dependencies
  mirrored to Harbor. `BROWSER_IMAGE` must be the Camofox runtime image.
- `UV_INDEX_URL` and `HF_ENDPOINT` — internal Python-package and Hugging Face
  mirrors used while producing the base image.

The 137 Harbor is HTTP, so `HARBOR_PLAIN_HTTP=true` is already the CI default.
Configure the GitLab Runner's Docker daemon to trust that registry as insecure.
For an HTTPS migration, override that value to `false`.

## Argo CD

Copy `argocd-application.yaml.example` into the GitOps administration repository,
replace its placeholders, and configure the Harbor pull secret in the target
namespace. Argo CD then observes the OCI chart version and owns deployment,
reconciliation, and rollback; CI only publishes artifacts.

The chart creates a PVC by default at `/data/zhiling` for Hermes configuration and
session state, matching the current all-in-one runtime image. The runtime image and the 137 K3s node are ARM64, so the chart
defaults `nodeSelector.kubernetes.io/arch` to `arm64`.
Set `persistence.existingClaim` to reuse a cluster-managed claim, or set
`persistence.enabled=false` only for disposable environments. The workspace is
an `emptyDir` by default; mount a workload-specific persistent workspace through
an environment-specific chart overlay if users need its files retained.

Runtime paths are configurable through `runtime.hermesHome`,
`runtime.stateDir`, and `runtime.defaultWorkspace`. Keep `runtime.stateDir`
inside `runtime.hermesHome` so it remains on the persistent volume. Use
`extraEnv` for non-sensitive runtime variables only.

When `browser.secret.enabled=true`, the chart injects `BROWSER_DESKTOP_PASSWORD`
and `BROWSER_RUNTIME_TOKEN_SECRET` from a Kubernetes Secret. Create that Secret
with the keys configured by `browser.secret.desktopPasswordKey` and
`browser.secret.runtimeTokenSecretKey`; never put real credentials into Git.
