# Development Guide: Main Feast Repository
> Please see [Development Guide](https://docs.feast.dev/project/development-guide) for project level development instructions.

## Overview
This guide is targeted at developers looking to contribute to Feast components in
the main Feast repository:
- [Feast Python SDK / CLI](#feast-python-sdk--cli)
- [Feast Java Serving](#feast-java-serving)
- [Feast Go Client](#feast-go-client)

## Community
See [Contribution process](https://docs.feast.dev/project/contributing) and [Community](https://docs.feast.dev/community) for details on how to get more involved in the community.

A quick few highlights:
- [RFCs](https://drive.google.com/drive/u/0/folders/0AAe8j7ZK3sxSUk9PVA)
- [Community Slack](https://slack.feast.dev/)
- [Feast Dev Mailing List](https://groups.google.com/g/feast-dev)
- [Community Calendar](https://calendar.google.com/calendar/u/0?cid=ZTFsZHVhdGM3MDU3YTJucTBwMzNqNW5rajBAZ3JvdXAuY2FsZW5kYXIuZ29vZ2xlLmNvbQ)
  - Includes biweekly community calls at 10AM PST

## Making a pull request
We use the convention that the assignee of a PR is the person with the next action.

This means that often, the assignee may be empty (if no reviewer has been found yet), the reviewer, or the PR writer if there are comments to be addressed.

### Pull request checklist
A quick list of things to keep in mind as you're making changes:
- As you make changes
  - Make your changes in a [forked repo](#forking-the-repo) (instead of making a branch on the main Feast repo)
  - [Sign your commits](#signing-off-commits) as you go (to avoid DCO checks failing)
  - [Rebase from master](#incorporating-upstream-changes-from-master) instead of using `git pull` on your PR branch
  - Install [pre-commit hooks](#pre-commit-hooks) to ensure all the default linters / formatters are run when you push.
- When you make the PR
  - Make a pull request from the forked repo you made
  - Ensure you add a GitHub **label** (i.e. a kind tag to the PR (e.g. `kind/bug` or `kind/housekeeping`)) or else checks will fail.
  - Ensure you leave a release note for any user facing changes in the PR. There is a field automatically generated in the PR request. You can write `NONE` in that field if there are no user facing changes.
  - Please run tests locally before submitting a PR (e.g. for Python, the [local integration tests](#local-integration-tests))
  - Try to keep PRs smaller. This makes them easier to review.

### Forking the repo
Fork the Feast Github repo and clone your fork locally. Then make changes to a local branch to the fork.

See [Creating a pull request from a fork](https://docs.github.com/en/github/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request-from-a-fork)

### Pre-commit Hooks
Setup [`pre-commit`](https://pre-commit.com/) to automatically lint and format the codebase on commit:
1. Ensure that you have Python (3.7 and above) with `pip`, installed.
2. Install `pre-commit` with `pip` &amp; install pre-push hooks
```sh
pip install pre-commit
pre-commit install --hook-type pre-commit --hook-type pre-push
```
3. On push, the pre-commit hook will run. This runs `make format` and `make lint`.

### Signing off commits
> :warning: Warning: using the default integrations with IDEs like VSCode or IntelliJ will not sign commits.
> When you submit a PR, you'll have to re-sign commits to pass the DCO check.

Use git signoffs to sign your commits. See
https://docs.github.com/en/github/authenticating-to-github/managing-commit-signature-verification for details

Then, you can sign off commits with the `-s` flag:
```
git commit -s -m "My first commit"
```

GPG-signing commits with `-S` is optional.

### Incorporating upstream changes from master
Our preference is the use of `git rebase [master]` instead of `git merge` : `git pull -r`.

Note that this means if you are midway through working through a PR and rebase, you'll have to force push:
`git push --force-with-lease origin [branch name]`

## Feast Python SDK / CLI
### Environment Setup
Setting up your development environment for Feast Python SDK / CLI:
1. Ensure that you have Docker installed in your environment. Docker is used to provision service dependencies during testing, and build images for feature servers and other components.
   1. Please note that we use [Docker with BuiltKit](https://docs.docker.com/develop/develop-images/build_enhancements/).
2. Ensure that you have `make`, Python (3.7 and above) with `pip`, installed.
3. _Recommended:_ Create a virtual environment to isolate development dependencies to be installed
```sh
# create & activate a virtual environment
python -m venv venv/
source venv/bin/activate
```

3. Upgrade `pip` if outdated
```sh
pip install --upgrade pip
```

4. (Optional): Install Node & Yarn. Then run the following to build Feast UI artifacts for use in `feast ui`
```
make build-ui
```

5. Install development dependencies for Feast Python SDK / CLI
```sh
pip install -e ".[dev]"
```

### Code Style & Linting
Feast Python SDK / CLI codebase:
- Conforms to [Black code style](https://black.readthedocs.io/en/stable/the_black_code_style.html)
- Has type annotations as enforced by `mypy`
- Has imports sorted by `isort`
- Is lintable by `flake8`

To ensure your Python code conforms to Feast Python code standards:
- Autoformat your code to conform to the code style:
```sh
make format-python
```

- Lint your Python code before submitting it for review:
```sh
make lint-python
```

> Setup [pre-commit hooks](#pre-commit-hooks) to automatically format and lint on commit.

### Unit Tests
Unit tests (`pytest`) for the Feast Python SDK / CLI can run as follows:
```sh
make test-python
```

> :warning: Local configuration can interfere with Unit tests and cause them to fail:
> - Ensure [no AWS configuration is present](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/configuration.html)
> and [no AWS credentials can be accessed](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html#configuring-credentials) by `boto3`
> - Ensure Feast Python SDK / CLI is not configured with configuration overrides (ie `~/.feast/config` should be empty).

### Integration Tests
There are two sets of tests you can run:
1. Local integration tests (for faster development, tests file offline store & key online stores)
2. Full integration tests (requires cloud environment setups)

#### Local integration tests
For this approach of running tests, you'll need to have docker set up locally: [Get Docker](https://docs.docker.com/get-docker/)

It leverages a file based offline store to test against emulated versions of Datastore, DynamoDB, and Redis, using ephemeral containers.

These tests create new temporary tables / datasets locally only, and they are cleaned up. when the containers are torn down.

```sh
make test-python-integration-local
```

#### Full integration tests
To test across clouds, on top of setting up Redis, you also need GCP / AWS / Snowflake setup.

> Note: you can manually control what tests are run today by inspecting
> [RepoConfiguration](https://github.com/feast-dev/feast/blob/master/sdk/python/tests/integration/feature_repos/repo_configuration.py)
> and commenting out tests that are added to `DEFAULT_FULL_REPO_CONFIGS`

**GCP**
1. Install the [Cloud SDK](https://cloud.google.com/sdk/docs/install).
2. Then run login to gcloud:
  ```
  gcloud auth login
  gcloud auth application-default login
  ```
- When you run `gcloud auth application-default login`, you should see some output of the form:
 ```
 Credentials saved to file: [$HOME/.config/gcloud/application_default_credentials.json]
 ```
- You should run `export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/application_default_credentials.json”` to add the application credentials to your .zshrc or .bashrc.
3. Run `export GCLOUD_PROJECT=[your project]` to your .zshrc or .bashrc.
4. Running `gcloud config list` should give you something like this:
```sh
$ gcloud config list
[core]
account = [your email]
disable_usage_reporting = True
project = [your project]

Your active configuration is: [default]
```
5. Export gcp specific environment variables. Namely,
```sh
export GCS_REGION='[your gcs region e.g US]'
export GCS_STAGING_LOCATION='[your gcs staging location]'
```

**AWS**
1. TODO(adchia): flesh out setting up AWS login (or create helper script)
2. To run the AWS Redshift and Dynamo integration tests you will have to export your own AWS credentials. Namely,

```sh
export AWS_REGION='[your aws region]'
export AWS_CLUSTER_ID='[your aws cluster id]'
export AWS_USER='[your aws user]'
export AWS_DB='[your aws database]'
export AWS_STAGING_LOCATION='[your s3 staging location uri]'
export AWS_IAM_ROLE='[redshift and s3 access role]'
export AWS_LAMBDA_ROLE='[your aws lambda execution role]'
export AWS_REGISTRY_PATH='[your aws registry path]'
```

**Snowflake**
1. See https://signup.snowflake.com/ to setup a trial.
2. Then to run successfully, you'll need some environment variables setup:
```sh
export SNOWFLAKE_CI_DEPLOYMENT='[snowflake_deployment]'
export SNOWFLAKE_CI_USER='[your user]'
export SNOWFLAKE_CI_PASSWORD='[your pw]'
export SNOWFLAKE_CI_ROLE='[your CI role e.g. SYSADMIN]'
export SNOWFLAKE_CI_WAREHOUSE='[your warehouse]'
```

Then run `make test-python-integration`. Note that for Snowflake / GCP / AWS, this will create new temporary tables / datasets.

#### Running specific provider tests or running your test against specific online or offline stores

1. If you don't need to have your test run against all of the providers(`gcp`, `aws`, and `snowflake`) or don't need to run against all of the online stores, you can tag your test with specific providers or stores that you need(`@pytest.mark.universal_online_stores` or `@pytest.mark.universal_online_stores` with the `only` parameter). The `only` parameter selects specific offline providers and online stores that your test will test against. Example:

```python
# Only parametrizes this test with the sqlite online store
@pytest.mark.universal_online_stores(only=["sqlite"])
def test_feature_get_online_features_types_match():
```

2. You can also filter tests to run by using pytest's cli filtering. Instead of using the make commands to test Feast, you can filter tests by name with the `-k` parameter. The parametrized integration tests are all uniquely identified by their provider and online store so the `-k` option can select only the tests that you need to run. For example, to run only Redshift related tests, you can use the following command:

```sh
python -m pytest -n 8 --integration -k Redshift sdk/python/tests
```

#### (Experimental) Run full integration tests against containerized services
Test across clouds requires existing accounts on GCP / AWS / Snowflake, and may incur costs when using these services.

For this approach of running tests, you'll need to have docker set up locally: [Get Docker](https://docs.docker.com/get-docker/)

It's possible to run some integration tests against emulated local versions of these services, using ephemeral containers.
These tests create new temporary tables / datasets locally only, and they are cleaned up. when the containers are torn down.

The services with containerized replacements currently implemented are:
- Datastore
- DynamoDB
- Redis
- Trino
- HBase
- Postgres

You can run `make test-python-integration-container` to run tests against the containerized versions of dependencies.


## Feast Java Serving
See [Java contributing guide](java/CONTRIBUTING.md)

## Feast Go Client
### Environment Setup
Setting up your development environment for Feast Go SDK:

- Install Golang, [`protoc` with the Golang &amp; grpc plugins](https://developers.google.com/protocol-buffers/docs/gotutorial#compiling-your-protocol-buffers)

### Building
Build the Feast Go Client with the `go` toolchain:
```sh
make go build
```

### Code Style & Linting
Feast Go Client codebase:
- Conforms to the code style enforced by `go fmt`.
- Is lintable by `go vet`.

Autoformat your Go code to satisfy the Code Style standard:
```sh
go fmt
```

Lint your Go code:
```sh
go vet
```

> Setup [pre-commit hooks](#pre-commit-hooks) to automatically format and lint on commit.

### Unit Tests
Unit tests for the Feast Go Client can be run as follows:
```sh
go test
```

### Testing with Github Actions workflows
* Update your current master on your forked branch and make a pull request against your own forked master.
* Enable workflows by going to actions and clicking `Enable Workflows`.
    * Pushes will now run your edited workflow yaml file against your test code.
    * Unfortunately, in order to test any github workflow changes, you must push the code to the branch and see the output in the actions tab.

## Issues
* pr-integration-tests workflow is skipped
    * Add `ok-to-test` github label.
* pr-integration-tests errors out with `Error: fatal: invalid refspec '+refs/pull//merge:refs/remotes/pull//merge'`
    * This is because github actions cannot pull the branch version for some reason so just find your PR number in your pull request header and hard code it into the `uses: actions/checkout@v2` section (i.e replace `refs/pull/${{ github.event.pull_request.number }}/merge` with `refs/pull/<pr number>/merge`)
* AWS/GCP workflow
    * Currently still cannot test GCP/AWS workflow without setting up secrets in a forked repository.
