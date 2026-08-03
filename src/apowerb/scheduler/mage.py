import json
from apowerb.configs.th2logger import setup_logging
from typing import Any
import requests
from apowerb.configs.settings import get_settings
from apowerb.scheduler.th2etl_client import OrchestratorUnavailable

logger = setup_logging(__name__)


settings = get_settings()


class MageAPIClient:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MageAPIClient, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.base_url = settings.base_url
        self.oauth_token = settings.oauth_token
        self.api_key = settings.api_key
        self.project_name = settings.project_name

        # Validate critical settings
        if not self.base_url:
            raise ValueError("Mage base_url is not configured in settings")
        if not self.api_key:
            raise ValueError("Mage api_key is not configured in settings")

        print("[Mage] API Client initialized:")
        print(f"   Base URL: {self.base_url}")
        print(f"   Project: {self.project_name}")
        print(f"   OAuth Token: {'Set' if self.oauth_token else 'Not Set'}")
        print(f"   API Key: {'Set' if self.api_key else 'Not Set'}")

        self._initialized = True

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self.api_key,
        }
        if self.oauth_token:
            headers["Cookie"] = f"oauth_token={self.oauth_token}"
        return headers

    def pipeline_exists(self, pipeline_uuid: str) -> bool:
        """Check if a pipeline exists."""
        try:
            url = f"{self.base_url}/api/pipelines/{pipeline_uuid}"
            response = requests.get(url, headers=self._get_headers(), timeout=10)

            if response.status_code == 200:
                data = response.json()
                if "error" in data:
                    return False
                return True
            return False
        except Exception as e:
            print(f"Error checking pipeline existence: {e}")
            return False

    def create_pipeline(
        self, pipeline_uuid: str, pipeline_type: str = "python"
    ) -> dict[str, Any] | None:
        """Create a new pipeline."""
        payload = {"pipeline": {"name": pipeline_uuid, "type": pipeline_type}}

        try:
            url = f"{self.base_url}/api/pipelines"
            response = requests.post(
                url, headers=self._get_headers(), json=payload, timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                if "error" in data:
                    print("❌ Failed to create pipeline.")
                    print(f"Response: {json.dumps(data, indent=2)}")
                    return None
                print(f"✅ Pipeline '{pipeline_uuid}' created successfully!")
                return data
            else:
                print(f"❌ Failed to create pipeline. Status: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"❌ Error creating pipeline: {e}")
            return None

    def block_exists(self, pipeline_uuid: str, block_uuid: str) -> bool:
        """Check if a block exists in a pipeline."""
        try:
            url = f"{self.base_url}/api/pipelines/{pipeline_uuid}/blocks/{block_uuid}"
            response = requests.get(url, headers=self._get_headers(), timeout=10)

            if response.status_code == 200:
                data = response.json()
                if "error" in data:
                    return False
                return "block" in data and data["block"] is not None
            return False
        except Exception as e:
            print(f"Error checking block existence: {e}")
            return False

    def create_block(
        self,
        pipeline_uuid: str,
        block_name: str,
        block_content: str,
        block_type: str = "data_loader",
    ) -> dict[str, Any] | None:
        """Create a new block in a pipeline."""
        payload = {
            "block": {
                "name": block_name,
                "type": block_type,
                "language": "python",
                "content": block_content,
                "priority": 0,
                "configuration": {"data_source": None},
            },
            "api_key": self.api_key,
        }

        try:
            url = f"{self.base_url}/api/pipelines/{pipeline_uuid}/blocks"
            response = requests.post(
                url, headers=self._get_headers(), json=payload, timeout=15
            )

            if response.status_code in [200, 201]:
                data = response.json()
                if "error" in data:
                    print("❌ Failed to create block.")
                    print(f"Response: {json.dumps(data, indent=2)}")
                    return None
                print(f"✅ Block '{block_name}' created successfully!")
                return data
            else:
                print(f"❌ Failed to create block. Status: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"❌ Error creating block: {e}")
            return None

    def get_pipeline_schedules(self, pipeline_uuid: str) -> list:
        """Get all pipeline schedules/triggers."""
        url = f"{self.base_url}/api/pipelines/{pipeline_uuid}/pipeline_schedules"
        try:
            response = requests.get(url, headers=self._get_headers(), timeout=15)
            response.raise_for_status()
            return response.json().get("pipeline_schedules", [])
        except Exception as e:
            print(f"Error fetching schedules: {e}")
            return []

    def get_all_pipelines(self) -> list:
        """Get all pipelines.

        Raises ``OrchestratorUnavailable`` rather than returning ``[]``: the
        caller in ``_initialize_pipeline`` was already written around an
        exception it never got, and printed "Successfully connected" over a
        dead Mage.
        """
        url = f"{self.base_url}/api/pipelines"
        try:
            response = requests.get(url, headers=self._get_headers(), timeout=15)
            response.raise_for_status()
            return response.json().get("pipelines", [])
        except Exception as e:
            print(f"Error fetching pipelines: {e}")
            raise OrchestratorUnavailable(
                f"Mage is unreachable at {self.base_url}: {e}"
            ) from e

    def get_pipeline_runs(self, schedule_id: int) -> list:
        """Get pipeline runs for a specific schedule."""
        url = f"{self.base_url}/api/pipeline_schedules/{schedule_id}/pipeline_runs"
        try:
            response = requests.get(url, headers=self._get_headers(), timeout=15)
            response.raise_for_status()
            return response.json().get("pipeline_runs", [])
        except Exception as e:
            print(f"Error fetching pipeline runs: {e}")
            return []

    def update_schedule_variables(
        self,
        schedule_id: int,
        variables: dict[str, Any],
    ) -> dict[str, Any] | None:
        """
        Update the runtime variables stored in a pipeline schedule.
        """
        url = f"{self.base_url}/api/pipeline_schedules/{schedule_id}?project={self.project_name}"

        payload = {
            "pipeline_schedule": {
                "variables": variables,
            },
            "api_key": self.api_key,
        }

        try:
            response = requests.put(
                url, headers=self._get_headers(), json=payload, timeout=30
            )
            response.raise_for_status()

            data = response.json()
            if "error" in data:
                print(f"❌ Failed to update schedule variables: {data.get('error')}")
                return None

            print(f"✅ Schedule variables updated for schedule_id: {schedule_id}")
            return data.get("pipeline_schedule", {})

        except Exception as e:
            print(f"❌ Error updating schedule variables: {e}")
            return None

    def update_schedule(
        self,
        schedule_id: int,
        schedule_interval: str | None = None,
        start_time: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Update a pipeline schedule's interval, start time, and/or status.
        """
        url = f"{self.base_url}/api/pipeline_schedules/{schedule_id}?project={self.project_name}"

        updates = {}
        if schedule_interval is not None:
            updates["schedule_interval"] = schedule_interval
        if start_time is not None:
            updates["start_time"] = start_time
        if status is not None:
            updates["status"] = status

        if not updates:
            return None

        payload = {
            "pipeline_schedule": updates,
            "api_key": self.api_key,
        }

        try:
            response = requests.put(
                url, headers=self._get_headers(), json=payload, timeout=30
            )
            response.raise_for_status()

            data = response.json()
            if "error" in data:
                print(f"Failed to update schedule: {data.get('error')}")
                return None

            print(f"Schedule updated for schedule_id: {schedule_id}")
            return data.get("pipeline_schedule", {})

        except Exception as e:
            print(f"Error updating schedule: {e}")
            return None

    def trigger_pipeline_run_for_schedule(
        self,
        schedule_id: int,
        run_variables: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """
        Create a pipeline run for a schedule (works for both API and time-based schedules).
        """
        url = f"{self.base_url}/api/pipeline_schedules/{schedule_id}/pipeline_runs?project={self.project_name}"

        payload = {
            "pipeline_run": {
                "pipeline_schedule_id": schedule_id,
                "variables": run_variables or {},
            },
            "api_key": self.api_key,
        }

        try:
            response = requests.post(
                url, headers=self._get_headers(), json=payload, timeout=30
            )
            response.raise_for_status()

            data = response.json()
            if "error" in data:
                print(f"❌ Failed to create pipeline run: {data.get('error')}")
                return None

            run_info = data.get("pipeline_run", {})
            run_id = run_info.get("id")
            status = run_info.get("status")

            print("✅ Pipeline run created!")
            print(f"   Run ID: {run_id}")
            print(f"   Status: {status}")

            return data
        except Exception as e:
            print(f"❌ Error creating pipeline run: {e}")
            return None

    def cancel_pipeline_run(self, run_id: int) -> dict[str, Any] | None:
        """Cancel a pipeline run by setting its status to cancelled."""
        url = f"{self.base_url}/api/pipeline_runs/{run_id}"
        payload = {"pipeline_run": {"status": "cancelled"}}
        try:
            response = requests.put(
                url, headers=self._get_headers(), json=payload, timeout=15
            )
            response.raise_for_status()
            return response.json().get("pipeline_run", {})
        except Exception as e:
            print(f"Error cancelling pipeline run: {e}")
            return None

    def get_pipeline_run(self, run_id: int) -> dict[str, Any] | None:
        """Get detailed info for a specific pipeline run."""
        url = f"{self.base_url}/api/pipeline_runs/{run_id}"
        try:
            response = requests.get(url, headers=self._get_headers(), timeout=15)
            response.raise_for_status()
            return response.json().get("pipeline_run", {})
        except Exception as e:
            print(f"Error fetching pipeline run: {e}")
            return None

    def get_pipeline_run_logs(self, run_id: int) -> list[dict[str, Any]]:
        """No-op: Mage has no per-run structured log endpoint in this app. Return
        an empty list so the shared dashboard route works under the default
        orchestrator without special-casing on the client type."""
        return []

    def create_schedule_trigger(
        self,
        pipeline_uuid: str,
        trigger_name: str,
        schedule_interval: str = "@hourly",
        runtime_variables: dict[str, Any] | None = None,
        start_time: str | None = None,  # Optional start time
    ) -> dict[str, Any] | None:
        """
        Create a SCHEDULE trigger for a pipeline.

        Schedule triggers run on a time-based schedule (cron or presets).

        IMPORTANT: Mage ignores start_time for cron-based schedules and fires
        immediately at the next cron slot. To honour a future start_time we
        create the trigger as INACTIVE and let the caller activate it at the
        right moment via update_schedule(status="active").

        Returns:
            Trigger info with schedule_id, status, starts_in_future flag, etc.
        """
        from datetime import datetime, timezone

        # Determine whether start_time is in the future
        starts_in_future = False
        if start_time:
            try:
                ts = start_time.rstrip("Z")
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                starts_in_future = dt > datetime.now(timezone.utc)
            except Exception:
                starts_in_future = False

        # Create inactive when start_time is in the future so Mage does not
        # fire at the first cron slot before the intended start_time.
        initial_status = "inactive" if starts_in_future else "active"

        url = f"{self.base_url}/api/pipelines/{pipeline_uuid}/pipeline_schedules?project={self.project_name}"

        payload = {
            "pipeline_schedule": {
                "name": trigger_name,
                "schedule_type": "time",
                "schedule_interval": schedule_interval,
                "start_time": start_time,
                "status": initial_status,
                "variables": runtime_variables or {},
                "sla": None,
                "settings": {},
            },
            "api_key": self.api_key,
        }

        try:
            response = requests.post(
                url, headers=self._get_headers(), json=payload, timeout=15
            )
            response.raise_for_status()

            data = response.json()
            if "error" in data:
                print(f"❌ Failed to create schedule trigger: {data.get('error')}")
                return None

            schedule_info = data.get("pipeline_schedule", {})
            schedule_id = schedule_info.get("id")

            print(f"✅ Schedule trigger '{trigger_name}' created!")
            print(f"   Schedule ID: {schedule_id}")
            print(f"   Interval: {schedule_interval}")
            print(f"   Start Time: {start_time or 'Immediately'}")
            print(f"   Status: {initial_status} {'(will activate at start_time)' if starts_in_future else ''}")

            return {
                "id": schedule_id,
                "name": trigger_name,
                "schedule_type": "time",
                "schedule_interval": schedule_interval,
                "start_time": start_time,
                "status": initial_status,
                "starts_in_future": starts_in_future,
                "variables": runtime_variables,
            }
        except Exception as e:
            print(f"❌ Error creating schedule trigger: {e}")
            return None

    def create_api_trigger(
        self,
        pipeline_uuid: str,
        trigger_name: str,
        runtime_variables: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Create an API trigger for a pipeline with runtime variables at trigger level."""
        url = f"{self.base_url}/api/pipelines/{pipeline_uuid}/pipeline_schedules?project={self.project_name}"

        payload = {
            "pipeline_schedule": {
                "name": trigger_name,
                "schedule_type": "api",
                "status": "active",
            }
        }

        # Add runtime variables at the trigger level
        if runtime_variables:
            payload["pipeline_schedule"]["variables"] = runtime_variables

        try:
            response = requests.post(
                url, headers=self._get_headers(), json=payload, timeout=15
            )
            data = response.json()

            if "error" in data:
                print("❌ Mage returned an error during trigger creation")
                print(json.dumps(data, indent=2))
                return None

            schedule = data.get("pipeline_schedule")
            if schedule and schedule.get("id") and schedule.get("token"):
                print(f"✅ API Trigger '{trigger_name}' created successfully!")
                if runtime_variables:
                    print(
                        f"   Runtime variables: {json.dumps(runtime_variables, indent=2)}"
                    )
                return schedule
            else:
                print("❌ Trigger creation failed")
                print(json.dumps(data, indent=2))
                return None
        except Exception as e:
            print(f"❌ Error creating API trigger: {e}")
            return None

    def trigger_pipeline(
        self,
        schedule_id: int,
        trigger_token: str,
        run_variables: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Trigger a pipeline execution (creates a new run)."""
        url = f"{self.base_url}/api/pipeline_schedules/{schedule_id}/pipeline_runs/{trigger_token}"

        payload = {}
        if run_variables:
            payload["pipeline_run"] = {"variables": run_variables}

        try:
            response = requests.post(
                url, headers=self._get_headers(), json=payload, timeout=15
            )
            data = response.json()

            if "error" in data:
                print("❌ Mage returned an error during pipeline trigger")
                print(json.dumps(data, indent=2))
                return None

            return data
        except Exception as e:
            print(f"❌ Error triggering pipeline: {e}")
            return None


class AgentOrchestrator:
    """Orchestrates Mage pipelines for agent execution."""

    PIPELINE_UUID = "agents"
    BLOCK_UUID = "agent_exe"
    BLOCK_TYPE = "data_loader"

    def __init__(self, client=None):
        # client defaults to MageAPIClient; a th2etl-backed client can be
        # injected (see get_orchestrator + the ORCHESTRATOR setting).
        self.client = client or MageAPIClient()
        self._pipeline_initialized = False

    def _ensure_pipeline_exists(self) -> bool:
        """Ensure the agents pipeline exists."""
        print(f"\n[1/2] Checking pipeline '{self.PIPELINE_UUID}'...")
        try:
            if self.client.pipeline_exists(self.PIPELINE_UUID):
                print(f"   ✓ Pipeline '{self.PIPELINE_UUID}' already exists.")
                return True

            print(f"   Pipeline not found. Creating '{self.PIPELINE_UUID}'...")
            result = self.client.create_pipeline(self.PIPELINE_UUID)
            if result:
                print(f"   ✓ Pipeline '{self.PIPELINE_UUID}' created successfully!")
                return True

            print(f"   ✗ Failed to create pipeline '{self.PIPELINE_UUID}'")
            print(f"   Debug: create_pipeline returned: {result}")
            return False
        except Exception as e:
            print(f"   ✗ Exception in _ensure_pipeline_exists: {e}")
            import traceback

            traceback.print_exc()
            return False

    def _ensure_block_exists(self) -> bool:
        """Ensure the agent_exe block exists in the pipeline."""
        print(f"\n[2/2] Checking block '{self.BLOCK_UUID}'...")
        try:
            if self.client.block_exists(self.PIPELINE_UUID, self.BLOCK_UUID):
                print(f"   ✓ Block '{self.BLOCK_UUID}' already exists.")
                return True

            print(f"   Block not found. Creating '{self.BLOCK_UUID}'...")

            # Create the block content that calls your API
            block_content = """import pandas as pd
import requests
from time import sleep

@data_loader
def load_agent_data(*args, **kwargs):
    # Access trigger-level runtime variables
    agent_id = kwargs.get('agent_id', 'unknown_agent')
    agent_meta = kwargs.get('agent_meta', {})
    jwt_token = kwargs.get('jwt_token')

    # Log the received variables
    print("AGENT EXECUTION STARTED")
    print(f"Agent ID: {agent_id}")
    print(f"Agent Meta: {agent_meta}")
    print(f"JWT Token Present: {bool(jwt_token)}")
    sleep(6)

    # return {"status":"success"}

    # API Configuration
    api_url = "PUBLIC_BASE_URL_PLACEHOLDER/api/adk/run_from_jwt"


    # Prepare headers with Bearer token
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Prepare payload
    payload = {
        "agent_id": agent_id,
        "data": agent_meta
    }

    try:
        # Send POST request
        response = requests.post(
            api_url,
            json=payload,
            headers=headers,
            timeout=300
        )

        response.raise_for_status()

        # Parse response
        data = response.json()

        # Convert to DataFrame (handle different response formats)
        if isinstance(data, list):
            result = pd.DataFrame(data)
        elif isinstance(data, dict):
            result = pd.DataFrame([data])
        else:
            result = pd.DataFrame([{"response": data}])

        print(f"✓ Request successful: {response.status_code}")
        return result

    except requests.exceptions.HTTPError as e:
        print(f"✗ HTTP Error: {e}")
        print(f"Response: {response.text}")
        return pd.DataFrame([{"error": str(e), "status": "failed"}])
    except Exception as e:
        print(f"✗ Error: {e}")
        return pd.DataFrame([{"error": str(e), "status": "failed"}])
"""

            block_content = block_content.replace(
                "PUBLIC_BASE_URL_PLACEHOLDER", settings.public_base_url.rstrip("/")
            )
            result = self.client.create_block(
                self.PIPELINE_UUID, self.BLOCK_UUID, block_content, self.BLOCK_TYPE
            )

            if result:
                print(f"   ✓ Block '{self.BLOCK_UUID}' created successfully!")
                return True

            print(f"   ✗ Failed to create block '{self.BLOCK_UUID}'")
            print(f"   Debug: create_block returned: {result}")
            return False
        except Exception as e:
            print(f"   ✗ Exception in _ensure_block_exists: {e}")
            import traceback

            traceback.print_exc()
            return False

    def _initialize_pipeline(self) -> bool:
        """Initialize pipeline infrastructure (pipeline + block)."""
        if self._pipeline_initialized:
            return True

        print("=" * 70)
        print("INITIALIZING AGENTS PIPELINE INFRASTRUCTURE")
        print("=" * 70)

        # Test Mage connectivity first
        print("\n[0/2] Testing Mage API connectivity...")
        try:
            pipelines = self.client.get_all_pipelines()
            print("   ✓ Successfully connected to Mage API")
            print(f"   Found {len(pipelines)} existing pipelines")
        except Exception as e:
            print(f"   ✗ Failed to connect to Mage API: {e}")
            print(f"   Base URL: {self.client.base_url}")
            print("   Check your Mage settings and network connectivity")
            return False

        # Step 1: Ensure pipeline exists
        if not self._ensure_pipeline_exists():
            return False

        # Step 2: Ensure block exists
        if not self._ensure_block_exists():
            return False

        self._pipeline_initialized = True
        print("\n✅ Pipeline infrastructure initialized successfully!")
        print("=" * 70)
        return True

    def create_agent_trigger(
        self,
        agent_id: str,
        agent_meta: dict[str, Any],
        create_initial_run: bool = False,
    ) -> dict[str, Any] | None:
        """ """
        logger.warning(
            "⚠️  create_agent_trigger() is deprecated! "
            "Schedule triggers are now created lazily on first /schedule_run call."
        )

        # For backward compatibility, still create API trigger if explicitly called
        # Ensure pipeline infrastructure exists
        if not self._initialize_pipeline():
            raise Exception("Failed to initialize pipeline infrastructure")

        agent_name = agent_meta.get("agent_name", agent_id)

        print("\n" + "=" * 70)
        print(f"⚠️  CREATING DEPRECATED API TRIGGER: {agent_name} (ID: {agent_id})")
        print("=" * 70)

        # Check if trigger already exists
        print(f"\n[3/3] Checking for API trigger '{agent_id}'...")
        schedules = self.client.get_pipeline_schedules(self.PIPELINE_UUID)
        existing_trigger = next(
            (s for s in schedules if s.get("name") == agent_id), None
        )

        if existing_trigger:
            schedule_id = existing_trigger.get("id")
            trigger_token = existing_trigger.get("token")
            print(f"   ✓ Trigger '{agent_id}' already exists.")
            print(f"   Schedule ID: {schedule_id}")
            print(f"   Trigger Token: {trigger_token}")
        else:
            # Create API trigger with agent_id and agent_meta as runtime variables
            print(f"   Creating API trigger '{agent_id}'...")

            runtime_variables = {
                "agent_id": agent_id,
                "agent_meta": agent_meta,
            }

            trigger_info = self.client.create_api_trigger(
                self.PIPELINE_UUID, agent_id, runtime_variables
            )

            if not trigger_info:
                raise Exception(f"Failed to create API trigger for agent '{agent_id}'")

            schedule_id = trigger_info.get("id")
            trigger_token = trigger_info.get("token")

            print(f"   Schedule ID: {schedule_id}")
            print(f"   Trigger Token: {trigger_token}")

        result = {
            "schedule_id": schedule_id,
            "trigger_token": trigger_token,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "agent_meta": agent_meta,
            "status": "trigger_ready",
            "type": "api_trigger_deprecated",
        }

        # Optionally trigger an initial run
        if create_initial_run:
            print("\n[4/4] Triggering initial pipeline run...")

            execution_result = self.client.trigger_pipeline(
                schedule_id, trigger_token, run_variables=None
            )

            if not execution_result:
                print("⚠ Warning: Failed to trigger initial run")
            else:
                run_info = execution_result.get("pipeline_run", {})
                run_id = run_info.get("id")
                status = run_info.get("status")

                result["run_id"] = run_id
                result["status"] = status

                print(f"\n✅ Initial run triggered! Run ID: {run_id}, Status: {status}")

        print("=" * 70)
        return result

    def create_schedule_trigger_for_agent(
        self,
        agent_id: str,
        agent_meta: dict[str, Any],
        schedule_interval: str = "@hourly",
        start_time: str | None = None,
        jwt_token: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Create a SCHEDULE trigger for an agent with optional start time.

        Returns:
            Schedule info with schedule_id, interval, start_time, etc.
        """
        # Ensure pipeline infrastructure exists
        if not self._initialize_pipeline():
            raise Exception("Failed to initialize pipeline infrastructure")

        agent_name = agent_meta.get("agent_name", agent_id)

        print("\n" + "=" * 70)
        print(f"✅ CREATING SCHEDULE TRIGGER: {agent_name} (ID: {agent_id})")
        print(f"   Interval: {schedule_interval}")
        print(
            f"   Start Time: {start_time or 'Immediately'}"
        )  # ✅ NEW: Show start time
        print("=" * 70)

        # Runtime variables for the schedule — agent_id and agent_meta are
        # stored at the trigger level; jwt_token is also baked in here so
        # that the very first scheduled run already has a valid token even
        # if the subsequent update_schedule_variables() call were to fail.
        runtime_variables = {
            "agent_id": agent_id,
            "agent_meta": agent_meta,
        }
        if jwt_token:
            runtime_variables["jwt_token"] = jwt_token

        # Create schedule trigger with start_time
        trigger_info = self.client.create_schedule_trigger(
            pipeline_uuid=self.PIPELINE_UUID,
            trigger_name=agent_id,  # Use agent_id as trigger name
            schedule_interval=schedule_interval,
            runtime_variables=runtime_variables,
            start_time=start_time,  # ✅ NEW: Pass start_time
        )

        if not trigger_info:
            raise Exception(f"Failed to create schedule trigger for agent '{agent_id}'")

        schedule_id = trigger_info.get("id")

        print("\n Schedule trigger created successfully!")
        print(f"   Schedule ID: {schedule_id}")
        print("   Type: time-based schedule")
        if start_time:
            print(
                f"   First run: {start_time}"
            )  # ✅ NEW: Show when first run will occur
        print("=" * 70)

        return {
            "schedule_id": schedule_id,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "agent_meta": agent_meta,
            "schedule_interval": schedule_interval,
            "start_time": start_time,  # ✅ NEW: Include start_time
            "schedule_type": "time",
            "status": "active",
        }


# Global orchestrator instance (singleton pattern)
_orchestrator_instance = None


def _build_client():
    """Pick the orchestrator client from the ORCHESTRATOR setting."""
    if settings.orchestrator == "th2etl":
        from apowerb.scheduler.th2etl_client import Th2etlAPIClient

        return Th2etlAPIClient(settings.th2etl_base_url)
    return MageAPIClient()


def get_orchestrator() -> AgentOrchestrator:
    """Get or create the global orchestrator instance."""
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = AgentOrchestrator(client=_build_client())
    return _orchestrator_instance


def process_agent_registration(
    agent_id: str, agent_meta: dict[str, Any], create_initial_run: bool = False
) -> dict[str, Any] | None:
    """
    This function creates API triggers which are now deprecated.
    """
    logger.warning(
        "  process_agent_registration() is deprecated! "
        "Do not use for new agents. Schedule triggers are created lazily on first /schedule_run."
    )

    orchestrator = get_orchestrator()
    return orchestrator.create_agent_trigger(
        agent_id, agent_meta, create_initial_run=create_initial_run
    )


def main():
    """Main entry point for testing."""
    print("\n" + "=" * 70)
    print("MAGE AGENT ORCHESTRATOR - TEST INTERFACE")
    print("=" * 70)

    # Initialize orchestrator
    # orchestrator = get_orchestrator()

    # Test agent registration
    while True:
        print("\n" + "-" * 70)
        agent_id = input("\nEnter Agent ID (or 'q' to quit): ").strip()

        if agent_id.lower() == "q":
            break

        agent_name = input("Enter Agent Name: ").strip()
        if not agent_name:
            print("⚠ Agent name is required")
            continue

        agent_meta_input = input(
            "Enter Agent Meta (JSON format, or press Enter for minimal): "
        ).strip()

        if agent_meta_input:
            try:
                agent_meta = json.loads(agent_meta_input)
            except json.JSONDecodeError:
                print("\n Invalid JSON, using as description")
                agent_meta = {"description": agent_meta_input}
        else:
            agent_meta = {}

        # Ensure agent_name is in metadata
        agent_meta["agent_name"] = agent_name

        # Create trigger
        try:
            result = process_agent_registration(
                agent_id, agent_meta, create_initial_run=True
            )
            if result:
                print(f"\n Agent registered! Trigger ready with UUID: {agent_id}")
            else:
                print("\n Agent registration failed. Check the errors above.")
        except Exception as e:
            print(f"\n Error: {e}")


if __name__ == "__main__":
    main()