//! The seed of one job VM: the cloud-init NoCloud files of design section
//! 4.4, item 3. `disks.rs` writes them into a VFAT image with the label
//! `CIDATA`.
//!
//! | File | What it holds |
//! |---|---|
//! | `meta-data` | the instance id of the job and the host name |
//! | `network-config` | the NIC by its MAC, the static address, the default route and the DNS server |
//! | `user-data` | `write_files` for `/etc/forgejo-runner/runner.json` and `/etc/forgejo-runner/job.env` |
//!
//! `forgejo-runner-job.service` of the image starts when both files of
//! `user-data` exist (`ci-runner-image/AGENTS.md`, "The job unit").
//!
//! Each file is JSON, and JSON is YAML for cloud-init. `serde_json` then
//! escapes each value, so no value can add a key or a line. `user-data`
//! starts with the line `#cloud-config`, which cloud-init needs.
//!
//! The runner token goes into `user-data` only. No other file, no path
//! and no error holds it.

use serde_json::{Value, json};

use crate::client::ForgejoOrigin;
use crate::names::{JobHandle, Labels, RunnerName};
use crate::net::GuestNetwork;
use crate::reply::Registration;

/// The file of the registration in the VM. `config.yml` of the image
/// names it in `runner.file`.
const RUNNER_FILE: &str = "/etc/forgejo-runner/runner.json";

/// The file of the job handle in the VM. The job unit reads it as its
/// `EnvironmentFile`.
const JOB_ENV_FILE: &str = "/etc/forgejo-runner/job.env";

/// The owner of the registration file: the job user of the image, who
/// runs `forgejo-runner`.
const RUNNER_FILE_OWNER: &str = "runner:runner";

/// Only the job user reads the registration, because it holds the token.
const RUNNER_FILE_MODE: &str = "0600";

/// The owner of the job file. systemd reads it as root.
const JOB_ENV_OWNER: &str = "root:root";

/// The job file holds no secret.
const JOB_ENV_MODE: &str = "0644";

/// The variable of the job file. The job unit passes it to
/// `one-job --handle`.
const JOB_HANDLE_VAR: &str = "JOB_HANDLE";

/// The scheme of each label in the registration file. `host` runs each
/// step in the VM itself, with no container. A label with no scheme gets
/// `docker` from `forgejo-runner`.
const LABEL_SCHEME: &str = "host";

/// The host name of each job VM.
const HOST_NAME: &str = "ci-job";

/// The prefix of the instance id. cloud-init runs its first-boot modules
/// once per instance id, so each job gets its own.
const INSTANCE_PREFIX: &str = "ci-job-";

/// The first line of `user-data`.
const CLOUD_CONFIG: &str = "#cloud-config\n";

/// The version of the `network-config` format: netplan version 2.
const NETWORK_VERSION: u8 = 2;

/// The name of the NIC in `network-config`. The MAC matches the NIC, so
/// the name is only the key of its entry.
const NIC_KEY: &str = "job0";

/// The name of each file of the seed, as cloud-init NoCloud reads it.
pub(crate) const META_DATA: &str = "meta-data";

/// See `META_DATA`.
pub(crate) const NETWORK_CONFIG: &str = "network-config";

/// See `META_DATA`.
pub(crate) const USER_DATA: &str = "user-data";

/// The ephemeral runner of one job, as `forgejo-runner` reads it from
/// `runner.json`: where Forgejo listens, the name and the labels that the
/// daemon chose, and the registration that Forgejo gave.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EphemeralRunner {
    /// Where the runner reaches Forgejo.
    origin: ForgejoOrigin,
    /// The name of the registration.
    name: RunnerName,
    /// The labels of the slot kind.
    labels: Labels,
    /// The id, the uuid and the token.
    registration: Registration,
}

impl EphemeralRunner {
    /// The runner `registration`, registered as `name` for the labels
    /// `labels`. The VM reaches Forgejo at `origin`.
    #[must_use]
    pub fn new(
        origin: ForgejoOrigin,
        name: RunnerName,
        labels: Labels,
        registration: Registration,
    ) -> Self {
        Self {
            origin,
            name,
            labels,
            registration,
        }
    }

    /// The `runner.json` of `forgejo-runner` v13: the fields of its
    /// `Registration` type. Each label gets the scheme `host`.
    fn file(&self) -> String {
        let labels: Vec<String> = self
            .labels
            .iter()
            .map(|label| format!("{label}:{LABEL_SCHEME}"))
            .collect();

        json!({
            "id": self.registration.id().get(),
            "uuid": self.registration.uuid().as_str(),
            "name": self.name.as_str(),
            "token": self.registration.token().reveal(),
            "address": self.origin.to_string(),
            "labels": labels,
        })
        .to_string()
    }
}

/// The seed of one job VM. It holds the runner and the handle of the job
/// that the runner takes, so no seed exists without a registration and a
/// job handle.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JobSeed {
    /// The runner of the job.
    runner: EphemeralRunner,
    /// The job that `one-job --handle` asks for.
    handle: JobHandle,
    /// The NIC, the address and the DNS server of the VM.
    network: GuestNetwork,
}

impl JobSeed {
    /// The seed of the VM that runs the job `handle` with `runner`, on
    /// the network `network`.
    #[must_use]
    pub fn new(runner: EphemeralRunner, handle: JobHandle, network: GuestNetwork) -> Self {
        Self {
            runner,
            handle,
            network,
        }
    }

    /// The handle of the job.
    #[must_use]
    pub fn handle(&self) -> &JobHandle {
        &self.handle
    }

    /// Each file of the seed: its name and its bytes.
    pub(crate) fn files(&self) -> [(&'static str, Vec<u8>); 3] {
        [
            (META_DATA, self.meta_data().into_bytes()),
            (NETWORK_CONFIG, self.network_config().into_bytes()),
            (USER_DATA, self.user_data().into_bytes()),
        ]
    }

    /// `meta-data`: the instance id and the host name.
    fn meta_data(&self) -> String {
        json!({
            "instance-id": format!("{INSTANCE_PREFIX}{}", self.handle),
            "local-hostname": HOST_NAME,
        })
        .to_string()
    }

    /// `network-config`: the NIC of the MAC gets the static address, the
    /// default route through the gateway and the DNS server.
    fn network_config(&self) -> String {
        let address = self.network.address();
        let nic = json!({
            "match": { "macaddress": self.network.mac().to_string() },
            "addresses": [address.to_string()],
            "routes": [{ "to": "default", "via": address.gateway().to_string() }],
            "nameservers": { "addresses": [self.network.dns().to_string()] },
        });

        let mut ethernets = serde_json::Map::new();
        ethernets.insert(NIC_KEY.to_owned(), nic);
        json!({ "version": NETWORK_VERSION, "ethernets": Value::Object(ethernets) }).to_string()
    }

    /// `user-data`: the two files of the job unit.
    fn user_data(&self) -> String {
        let config = json!({
            "write_files": [
                {
                    "path": RUNNER_FILE,
                    "owner": RUNNER_FILE_OWNER,
                    "permissions": RUNNER_FILE_MODE,
                    "content": self.runner.file(),
                },
                {
                    "path": JOB_ENV_FILE,
                    "owner": JOB_ENV_OWNER,
                    "permissions": JOB_ENV_MODE,
                    "content": format!("{JOB_HANDLE_VAR}={}\n", self.handle),
                },
            ],
        });
        format!("{CLOUD_CONFIG}{config}\n")
    }
}

#[cfg(test)]
mod tests {
    use std::net::Ipv4Addr;

    use serde_json::{Value, json};

    use super::{EphemeralRunner, JobSeed, META_DATA, NETWORK_CONFIG, USER_DATA};
    use crate::names::{Label, Labels};
    use crate::net::{BridgeNet, GuestNetwork};
    use crate::reply::Registration;

    /// The token of the fake registration.
    const TOKEN: &str = "3f9a0c1e7b2d4f6a8c0e1b3d5f7a9c2e4b6d8f0a";

    /// The handle of the fake job.
    const HANDLE: &str = "2f923f44-2d31-4d08-ad46-e04a543826e0";

    /// A seed of a fake registration, as Forgejo 17.0.1 writes one.
    fn seed() -> JobSeed {
        let registration: Registration = serde_json::from_value(json!({
            "id": 3,
            "uuid": "a4087518-6f64-42b1-8383-c37ab78396af",
            "token": TOKEN,
        }))
        .unwrap();
        let labels =
            Labels::new(["ubuntu-24.04", "kvm"].map(|text| text.parse::<Label>().unwrap()))
                .unwrap();
        let runner = EphemeralRunner::new(
            "http://192.0.2.10:3000".parse().unwrap(),
            "ci-job-1".parse().unwrap(),
            labels,
            registration,
        );

        let bridge = BridgeNet::new(Ipv4Addr::new(192, 0, 2, 1)).unwrap();
        let network = GuestNetwork::new(
            "52:54:00:c1:00:02".parse().unwrap(),
            bridge.guest(Ipv4Addr::new(192, 0, 2, 2)).unwrap(),
            Ipv4Addr::new(198, 51, 100, 53),
        )
        .unwrap();
        JobSeed::new(runner, HANDLE.parse().unwrap(), network)
    }

    /// The file `name` of the seed, as text.
    fn file(seed: &JobSeed, name: &str) -> String {
        let files = seed.files();
        let (_, bytes) = files.iter().find(|(file, _)| *file == name).unwrap();
        String::from_utf8(bytes.clone()).unwrap()
    }

    #[test]
    fn the_meta_data_names_an_instance_of_the_job() {
        let meta: Value = serde_json::from_str(&file(&seed(), META_DATA)).unwrap();
        assert_eq!(
            meta,
            json!({
                "instance-id": format!("ci-job-{HANDLE}"),
                "local-hostname": "ci-job",
            })
        );
    }

    #[test]
    fn the_network_config_matches_the_mac_and_sets_the_address() {
        let network: Value = serde_json::from_str(&file(&seed(), NETWORK_CONFIG)).unwrap();
        assert_eq!(
            network,
            json!({
                "version": 2,
                "ethernets": {
                    "job0": {
                        "match": { "macaddress": "52:54:00:c1:00:02" },
                        "addresses": ["192.0.2.2/24"],
                        "routes": [{ "to": "default", "via": "192.0.2.1" }],
                        "nameservers": { "addresses": ["198.51.100.53"] },
                    },
                },
            })
        );
    }

    #[test]
    fn the_user_data_writes_the_runner_file_and_the_job_file() {
        let text = file(&seed(), USER_DATA);
        let body = text.strip_prefix("#cloud-config\n").unwrap();
        let config: Value = serde_json::from_str(body).unwrap();

        let Some([runner, job]) = config["write_files"].as_array().map(Vec::as_slice) else {
            panic!("{config}");
        };
        assert_eq!(runner["path"], "/etc/forgejo-runner/runner.json");
        assert_eq!(runner["owner"], "runner:runner");
        assert_eq!(runner["permissions"], "0600");
        let registration: Value =
            serde_json::from_str(runner["content"].as_str().unwrap()).unwrap();
        assert_eq!(
            registration,
            json!({
                "id": 3,
                "uuid": "a4087518-6f64-42b1-8383-c37ab78396af",
                "name": "ci-job-1",
                "token": TOKEN,
                "address": "http://192.0.2.10:3000",
                "labels": ["kvm:host", "ubuntu-24.04:host"],
            })
        );

        assert_eq!(
            job,
            &json!({
                "path": "/etc/forgejo-runner/job.env",
                "owner": "root:root",
                "permissions": "0644",
                "content": format!("JOB_HANDLE={HANDLE}\n"),
            })
        );
    }

    #[test]
    fn only_the_user_data_holds_the_token() {
        let seed = seed();
        for (name, bytes) in seed.files() {
            let holds = String::from_utf8(bytes).unwrap().contains(TOKEN);
            assert_eq!(holds, name == USER_DATA, "{name}");
        }
        assert!(!format!("{seed:?}").contains(TOKEN));
    }
}
