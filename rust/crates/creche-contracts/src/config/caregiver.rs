//! The config of the caregiver, the family manager.
//!
//! `systemd/creche-caregiver.service` starts `caregiver serve` with an
//! argument list, with the variables of `managerd.env` and with the site
//! file. The unit puts three variables into the argument list:
//! `AGENT_SANDBOX_IMAGE`, `AGENT_SANDBOX_PYTHON_IMAGE` and `AGENT_PEP_URL`.
//! The service itself reads two variables: `AGENT_LAN_ADDRESS` and
//! `LITELLM_MASTER_KEY`.
//!
//! The Python reader is `caregiver.cli`, an `argparse` parser. It has no
//! entry point that takes a map, so no vector covers this config.
//! [`RawServe`] is the raw form: the text of each flag, as an argument
//! parser gives it.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use super::values::{DirPath, FilePath, HttpUrl, LanAddress, Port, Seconds};
use super::{
    AtReload, AtStart, AttendanceTarget, Checked, ConfigError, ConfigErrors, Env, FailureAction,
    LAN_ADDRESS, Parsed, ProcessConfig, all2, all3, all4, pytext,
};
use crate::secret::Secret;

/// The variable that holds the master key of LiteLLM. The key is never on
/// an argument list (invariant 13).
pub const MASTER_KEY: &str = "LITELLM_MASTER_KEY";

const DEFAULT_STATE_ROOT: &str = "/srv/agents/state/rework";
const DEFAULT_RELEASE_ROOT: &str = "/var/lib/creche-handover";

/// The file that a `playpen` release installs. Its references win over the
/// two image flags.
const RELEASED_IMAGES: &str = "/opt/components/playpen/images.env";

const LITELLM_PORT: u16 = 4000;
const PEP_PORT: u16 = 8300;
const ATTENDANCE_PORT: u16 = 8350;

const DEFAULT_POLL_INTERVAL_S: f64 = 2.0;
const DEFAULT_STOP_GRACE_S: f64 = 330.0;
const DEFAULT_PEP_PROBE_INTERVAL_S: f64 = 10.0;
const DEFAULT_PEP_UNREACHABLE_AFTER_S: f64 = 90.0;

/// The count of families that converge at one time when the flag is not
/// set.
const DEFAULT_MAX_PASSES: u16 = 4;

/// The count of hexadecimal digits in a SHA-256 digest.
const DIGEST_HEX: usize = 64;

/// What separates the name of an image from its digest.
const DIGEST_MARK: &str = "@sha256:";

// CONTRACT-QUESTION: contract 01 §3.9 and contract 06 say that the platform
// names the image of each flavor, and give the name no grammar. The Python
// service takes each text for `--image`. `caregiver.released.REFERENCE_RE`
// is the one Python grammar of an image reference, for the file of a
// `playpen` release. The type takes that grammar: a reference with a
// digest. A laxer reading lets a tag, which can move, select the code of a
// sandbox.
/// The reference of one sandbox image: `<registry>/<name>@sha256:<digest>`.
///
/// The reference names an image by its digest, so the image cannot change
/// under the name (invariant 10).
///
/// ```
/// use creche_contracts::config::caregiver::ImageRef;
///
/// let text = format!("registry.example/playpen@sha256:{}", "0123456789abcdef".repeat(4));
/// let image: ImageRef = text.parse()?;
/// assert_eq!(image.as_str(), text);
/// assert!("registry.example/playpen:latest".parse::<ImageRef>().is_err());
/// # Ok::<(), creche_contracts::config::caregiver::ImageRefError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::caregiver::ImageRef;
///
/// let image = ImageRef(String::from("playpen:latest"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ImageRef(String);

impl ImageRef {
    /// The reference as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for ImageRef {
    type Err = ImageRefError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let (name, digest) = text
            .split_once(DIGEST_MARK)
            .ok_or(ImageRefError::NoDigest)?;
        let lower_hex = |byte: u8| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f');
        if digest.len() != DIGEST_HEX || !digest.bytes().all(lower_hex) {
            return Err(ImageRefError::NoDigest);
        }

        let plain = |byte: u8| byte.is_ascii_lowercase() || byte.is_ascii_digit();
        let (registry, path) = name.split_once('/').ok_or(ImageRefError::BadName)?;
        let registry_holds = !registry.is_empty()
            && registry
                .bytes()
                .all(|byte| plain(byte) || matches!(byte, b'.' | b':' | b'-'));
        let path_holds = path.bytes().next().is_some_and(plain)
            && path
                .bytes()
                .all(|byte| plain(byte) || matches!(byte, b'.' | b'_' | b'/' | b'-'));
        if !registry_holds || !path_holds {
            return Err(ImageRefError::BadName);
        }

        Ok(Self(text.to_owned()))
    }
}

/// Why a text is not the reference of an image.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImageRefError {
    /// The text does not end with `@sha256:` and 64 hexadecimal digits in
    /// lower case.
    NoDigest,
    /// The text before the digest is not `<registry>/<name>` in lower case.
    BadName,
}

impl fmt::Display for ImageRefError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoDigest => f.write_str("an image reference ends with @sha256: and 64 digits"),
            Self::BadName => f.write_str("an image reference starts with <registry>/<name>"),
        }
    }
}

impl Error for ImageRefError {}

/// The text of each flag of `caregiver serve`, as an argument parser gives
/// it: the raw form. `None` stands for a flag that the command line does
/// not hold.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RawServe {
    /// The registry root: the one positional argument.
    pub registry: String,
    /// `--image`: the image of the flavor `base`.
    pub image: String,
    /// `--image-python`: the image of the flavor `python`.
    pub image_python: Option<String>,
    /// `--released-images`: the file of a `playpen` release.
    pub released_images: Option<String>,
    /// `--state-root`
    pub state_root: Option<String>,
    /// `--release-root`
    pub release_root: Option<String>,
    /// `--litellm-base-url`
    pub litellm_base_url: Option<String>,
    /// `--sessiond-url`
    pub sessiond_url: Option<String>,
    /// `--sessiond-socket`. It wins over `--sessiond-url`.
    pub sessiond_socket: Option<String>,
    /// `--pep-url`. The empty text turns the watch off.
    pub pep_url: Option<String>,
    /// `--pep-probe-interval-s`
    pub pep_probe_interval_s: Option<String>,
    /// `--pep-unreachable-after-s`
    pub pep_unreachable_after_s: Option<String>,
    /// `--poll-interval-s`
    pub poll_interval_s: Option<String>,
    /// `--max-concurrent-passes`
    pub max_concurrent_passes: Option<String>,
    /// `--stop-grace-s`
    pub stop_grace_s: Option<String>,
    /// `--write`: act. Without it the service prints its plan and exits.
    pub write: bool,
}

/// Whether the service acts or only prints its plan.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// Print the plan and exit.
    Plan,
    /// Watch the registry and converge each family.
    Write,
}

/// The watch of the chaperone (contract 05 §3.3).
#[derive(Debug, Clone, PartialEq)]
pub enum PepWatch {
    /// `--pep-url ''`. Each status document then says `pep.watch: off`.
    Off,
    /// The caregiver probes `/healthz` of the chaperone.
    On {
        /// Where the chaperone answers.
        url: HttpUrl,
        /// The time between two probes.
        interval: Seconds,
        /// The time of silence after which each family gets a fault.
        unreachable_after: Seconds,
    },
}

/// The images that the caregiver creates a sandbox from, by flavor
/// (contract 01 §3.9).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Images {
    base: ImageRef,
    python: Option<ImageRef>,
    released: Option<FilePath>,
}

impl Images {
    /// The image of the flavor `base`.
    #[must_use]
    pub fn base(&self) -> &ImageRef {
        &self.base
    }

    /// The image of the flavor `python`. `None` when the host has none: a
    /// family that asks for it then gets a fault and no sandbox.
    #[must_use]
    pub fn python(&self) -> Option<&ImageRef> {
        self.python.as_ref()
    }

    /// The file that a `playpen` release installs. Its references win over
    /// the two flags. `None` for a run with a state root of its own.
    #[must_use]
    pub fn released(&self) -> Option<&FilePath> {
        self.released.as_ref()
    }
}

/// The config of `caregiver serve`.
///
/// FAILURE ACTION. At start, a config that is not valid stops the daemon
/// with `EX_CONFIG`, and the unit file must hold
/// `RestartPreventExitStatus=78`. The Python service exits with 2 for no
/// LAN address and for a flag that `argparse` refuses, so systemd starts
/// the unit again after each exit, with no limit. The daemon does not read
/// this config again: a `SIGHUP` only starts a look at the registry.
///
/// The type reads the text of a flag as `argparse` does, with no strip. The
/// empty text reads as a flag that is not set for three flags only:
/// `--image-python`, `--litellm-base-url` and `--sessiond-url`. An empty
/// `--pep-url` turns the watch off. The master key is the exact text of its
/// variable.
///
/// The type is stricter than the Python reader in these places:
///
/// 1. An image is a reference with a digest. `argparse` takes each text.
/// 2. Each path is absolute. `argparse` takes each text as a path, and reads
///    the empty text as the directory `.`.
/// 3. Each URL is an [`HttpUrl`]. The Python service takes each text.
/// 4. Each count of seconds is finite and more than zero. `argparse` takes
///    each number that `float` reads: zero, a negative number, `nan` and
///    `inf` too.
/// 5. `--max-concurrent-passes` is 1 to 65535. `argparse` takes each
///    integer.
/// 6. A state root of its own needs a release root of its own in the two
///    modes. The Python service makes that check only with `--write`.
/// 7. A LAN address that is set must be a [`LanAddress`], also when each
///    plane has a flag. The Python service then does not read the variable.
///
/// `LITELLM_MASTER_KEY` is necessary for `--write`. The Python service
/// refuses a start without it too.
///
/// ```
/// use creche_contracts::config::caregiver::{CaregiverConfig, PepWatch, RawServe};
/// use creche_contracts::config::Env;
///
/// let raw = RawServe {
///     registry: String::from("/srv/agents/registry"),
///     image: format!("registry.example/playpen@sha256:{}", "0123456789abcdef".repeat(4)),
///     pep_url: Some(String::new()),
///     ..RawServe::default()
/// };
/// let env = Env::from_pairs([("AGENT_LAN_ADDRESS", "192.0.2.10")]);
/// let config = CaregiverConfig::from_parts(&raw, &env)?;
/// assert_eq!(config.pep_watch(), &PepWatch::Off);
/// assert_eq!(config.litellm_url().as_str(), "http://192.0.2.10:4000");
/// # Ok::<(), creche_contracts::config::ConfigErrors>(())
/// ```
///
/// Code outside this module cannot build a config from raw parts, and cannot
/// change a field of a valid config:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::caregiver::{CaregiverConfig, PepWatch, RawServe};
/// use creche_contracts::config::Env;
///
/// let raw = RawServe {
///     registry: String::from("/srv/agents/registry"),
///     image: format!("registry.example/playpen@sha256:{}", "0123456789abcdef".repeat(4)),
///     pep_url: Some(String::new()),
///     ..RawServe::default()
/// };
/// let env = Env::from_pairs([("AGENT_LAN_ADDRESS", "192.0.2.10")]);
/// let config = CaregiverConfig::from_parts(&raw, &env).unwrap();
/// let other = CaregiverConfig { max_passes: 0, ..config };
/// ```
#[derive(Debug)]
pub struct CaregiverConfig {
    registry: DirPath,
    state_root: DirPath,
    release_root: DirPath,
    images: Images,
    litellm_url: HttpUrl,
    master_key: Option<Secret>,
    attendance: AttendanceTarget,
    pep_watch: PepWatch,
    poll_interval: Seconds,
    max_passes: u16,
    stop_grace: Seconds,
    mode: Mode,
}

impl Checked for CaregiverConfig {
    const FAILURE: FailureAction = FailureAction::new(AtStart::ExitConfig, AtReload::NotRead);
}

impl ProcessConfig for CaregiverConfig {
    const UNIT: &'static str = "creche-caregiver.service";
}

/// The text of a flag that the command line holds, with no strip.
/// `argparse` gives that text to the type of the flag, the empty text too.
fn given(text: Option<&String>) -> Option<&str> {
    text.map(String::as_str)
}

/// The text of a flag for which the Python service reads the empty text as
/// a flag that is not set: `--image-python`, `--litellm-base-url` and
/// `--sessiond-url`.
fn or_unset(text: Option<&String>) -> Option<&str> {
    given(text).filter(|text| !text.is_empty())
}

/// The value of a flag as a `T`.
fn parse<T: super::Value>(flag_name: &'static str, text: &str) -> Parsed<T> {
    text.parse()
        .map_err(|error| T::refuse(flag_name, error).into())
}

/// The value of a flag as a `T`, or `default` for a flag that is not set.
fn parse_or<T: super::Value>(
    flag_name: &'static str,
    text: Option<&String>,
    default: &str,
) -> Parsed<T> {
    parse(flag_name, given(text).unwrap_or(default))
}

/// A count of seconds of a flag, or the default of the code.
fn seconds(flag_name: &'static str, text: Option<&String>, default: f64) -> Parsed<Seconds> {
    let refuse = |error| ConfigError::Seconds {
        variable: flag_name,
        error,
    };

    match given(text) {
        Some(text) => text.parse().map_err(|error| refuse(error).into()),
        None => Seconds::try_from(default).map_err(|error| refuse(error).into()),
    }
}

/// The LAN address of the site file as the parse gives it: the address,
/// `None` for a site that gives none, or the error of a value that is not
/// valid.
type Lan = Parsed<Option<LanAddress>>;

/// The URL of one plane on the LAN address. The address has no default.
///
/// Each plane that needs the address reports the same error. The caller
/// keeps that error one time.
fn on_lan(lan: &Lan, port: u16) -> Parsed<HttpUrl> {
    let unset = ConfigError::Unset {
        variable: LAN_ADDRESS,
    };
    let port = Port::fixed(port).ok_or(unset)?;

    match lan {
        Ok(Some(address)) => Ok(HttpUrl::on_lan(address, port)),
        Ok(None) => Err(unset.into()),
        Err(errors) => Err(errors.clone()),
    }
}

/// The URL of a flag, or the URL of the plane on the LAN address. `text` is
/// `None` for a flag that is not set.
fn url_or_lan(
    flag_name: &'static str,
    text: Option<&str>,
    lan: &Lan,
    port: u16,
) -> Parsed<HttpUrl> {
    match text {
        Some(text) => parse(flag_name, text),
        None => on_lan(lan, port),
    }
}

fn images(raw: &RawServe, state_root: &Parsed<DirPath>) -> Parsed<Images> {
    let image = |flag_name, text: &str| {
        text.parse::<ImageRef>().map_err(|_| {
            ConfigErrors::from(ConfigError::BadForm {
                variable: flag_name,
                form: "an image reference with a digest",
            })
        })
    };
    let python = or_unset(raw.image_python.as_ref())
        .map(|text| image("--image-python", text))
        .transpose();
    // The default file is the file of the real plane. A run with a state
    // root of its own reads no released file unless the flag names one.
    let on_real_plane = state_root
        .as_ref()
        .is_ok_and(|root| root.as_str() == DEFAULT_STATE_ROOT);
    let released = match (given(raw.released_images.as_ref()), on_real_plane) {
        (Some(text), _) => parse("--released-images", text).map(Some),
        (None, true) => parse("--released-images", RELEASED_IMAGES).map(Some),
        (None, false) => Ok(None),
    };
    let (base, python, released) = all3(image("--image", &raw.image), python, released)?;

    Ok(Images {
        base,
        python,
        released,
    })
}

fn attendance(raw: &RawServe, lan: &Lan) -> Parsed<AttendanceTarget> {
    if let Some(socket) = given(raw.sessiond_socket.as_ref()) {
        return parse("--sessiond-socket", socket).map(AttendanceTarget::Socket);
    }

    url_or_lan(
        "--sessiond-url",
        or_unset(raw.sessiond_url.as_ref()),
        lan,
        ATTENDANCE_PORT,
    )
    .map(AttendanceTarget::Url)
}

fn pep_watch(raw: &RawServe, lan: &Lan) -> Parsed<PepWatch> {
    // `argparse` parses the two times also when the watch is off.
    let times = all2(
        seconds(
            "--pep-probe-interval-s",
            raw.pep_probe_interval_s.as_ref(),
            DEFAULT_PEP_PROBE_INTERVAL_S,
        ),
        seconds(
            "--pep-unreachable-after-s",
            raw.pep_unreachable_after_s.as_ref(),
            DEFAULT_PEP_UNREACHABLE_AFTER_S,
        ),
    );
    let pep_url = given(raw.pep_url.as_ref());
    if pep_url.is_some_and(str::is_empty) {
        return times.map(|_| PepWatch::Off);
    }

    let url = url_or_lan("--pep-url", pep_url, lan, PEP_PORT);
    let (url, (interval, unreachable_after)) = all2(url, times)?;

    Ok(PepWatch::On {
        url,
        interval,
        unreachable_after,
    })
}

fn max_passes(raw: &RawServe) -> Parsed<u16> {
    let Some(text) = given(raw.max_concurrent_passes.as_ref()) else {
        return Ok(DEFAULT_MAX_PASSES);
    };
    let passes = match pytext::integer(text) {
        Some(pytext::Integer::Fits(passes)) => u16::try_from(passes).ok(),
        Some(pytext::Integer::TooLarge) | None => None,
    };

    passes.filter(|passes| *passes > 0).ok_or_else(|| {
        ConfigError::NotInRange {
            variable: "--max-concurrent-passes",
            min: 1,
            max: i64::from(u16::MAX),
        }
        .into()
    })
}

// CONTRACT-QUESTION: `spec.md` §5.4 says where the master key of LiteLLM
// lives, and not what the caregiver does without it. The Python service
// refuses `--write` with no key, and the type does the same: the service
// cannot mint a family key without it. The laxer reading gives a daemon
// that runs and converges no family. To take it, let `Mode::Write` hold no
// key.
/// The master key as its variable holds it. The Python service reads the
/// variable with no strip. `None` for a variable that is not set or empty.
fn master_key(env: &Env) -> Parsed<Option<Secret>> {
    let Some(text) = env.exact(MASTER_KEY)?.filter(|text| !text.is_empty()) else {
        return Ok(None);
    };

    match Secret::try_from(text.to_owned()) {
        Ok(key) => Ok(Some(key)),
        Err(error) => Err(ConfigError::Secret {
            variable: MASTER_KEY,
            error,
        }
        .into()),
    }
}

/// The release root. A state root of its own needs a release root of its
/// own: a scratch run must not file a request into the spool that root
/// drains.
fn release_root(raw: &RawServe, state_root: &Parsed<DirPath>) -> Parsed<DirPath> {
    let root: DirPath = parse_or(
        "--release-root",
        raw.release_root.as_ref(),
        DEFAULT_RELEASE_ROOT,
    )?;
    let scratch_state = state_root
        .as_ref()
        .is_ok_and(|state| state.as_str() != DEFAULT_STATE_ROOT);
    if scratch_state && root.as_str() == DEFAULT_RELEASE_ROOT {
        return Err(ConfigError::BadForm {
            variable: "--release-root",
            form: "a release root of its own beside a state root of its own",
        }
        .into());
    }

    Ok(root)
}

impl CaregiverConfig {
    /// Parses the flags and the two variables. The function collects each
    /// error, and an error that two parts report is in the list one time.
    /// An error names the flag, for example `--image`, or the variable.
    ///
    /// # Errors
    ///
    /// [`ConfigErrors`] holds one error for each flag and each variable
    /// that the service cannot use.
    pub fn from_parts(raw: &RawServe, env: &Env) -> Result<Self, ConfigErrors> {
        let lan = env.parse::<LanAddress>(LAN_ADDRESS);
        let state_root = parse_or("--state-root", raw.state_root.as_ref(), DEFAULT_STATE_ROOT);
        let mode = if raw.write { Mode::Write } else { Mode::Plan };
        let master_key = match (master_key(env), mode) {
            (Ok(None), Mode::Write) => Err(ConfigError::Unset {
                variable: MASTER_KEY,
            }
            .into()),
            (key, _) => key,
        };
        let roots = all3(
            parse("--registry", &raw.registry),
            release_root(raw, &state_root),
            images(raw, &state_root),
        );
        let planes = all4(
            url_or_lan(
                "--litellm-base-url",
                or_unset(raw.litellm_base_url.as_ref()),
                &lan,
                LITELLM_PORT,
            ),
            master_key,
            attendance(raw, &lan),
            pep_watch(raw, &lan),
        );
        // An address that is set and not valid is an error, also when each
        // plane has a flag.
        let site = lan.map(|_| ());
        let pace = all3(
            seconds(
                "--poll-interval-s",
                raw.poll_interval_s.as_ref(),
                DEFAULT_POLL_INTERVAL_S,
            ),
            max_passes(raw),
            seconds(
                "--stop-grace-s",
                raw.stop_grace_s.as_ref(),
                DEFAULT_STOP_GRACE_S,
            ),
        );
        let parsed = all4(all2(state_root, roots), site, planes, pace);
        let ((state_root, roots), (), planes, pace) = parsed.map_err(ConfigErrors::each_once)?;
        let (registry, release_root, images) = roots;
        let (litellm_url, master_key, attendance, pep_watch) = planes;
        let (poll_interval, max_passes, stop_grace) = pace;

        Ok(Self {
            registry,
            state_root,
            release_root,
            images,
            litellm_url,
            master_key,
            attendance,
            pep_watch,
            poll_interval,
            max_passes,
            stop_grace,
            mode,
        })
    }

    /// The registry checkout.
    #[must_use]
    pub fn registry(&self) -> &DirPath {
        &self.registry
    }

    /// The root of the state of the platform.
    #[must_use]
    pub fn state_root(&self) -> &DirPath {
        &self.state_root
    }

    /// The root of root's own files: the release spool, the sealed secrets
    /// and the roster.
    #[must_use]
    pub fn release_root(&self) -> &DirPath {
        &self.release_root
    }

    /// The images of the sandboxes.
    #[must_use]
    pub fn images(&self) -> &Images {
        &self.images
    }

    /// Where LiteLLM answers.
    #[must_use]
    pub fn litellm_url(&self) -> &HttpUrl {
        &self.litellm_url
    }

    /// The master key of LiteLLM. `None` only for [`Mode::Plan`].
    #[must_use]
    pub fn master_key(&self) -> Option<&Secret> {
        self.master_key.as_ref()
    }

    /// Where `attendance` answers.
    #[must_use]
    pub fn attendance(&self) -> &AttendanceTarget {
        &self.attendance
    }

    /// The watch of the chaperone.
    #[must_use]
    pub fn pep_watch(&self) -> &PepWatch {
        &self.pep_watch
    }

    /// The time between two looks at the registry.
    #[must_use]
    pub fn poll_interval(&self) -> Seconds {
        self.poll_interval
    }

    /// The count of families that converge at one time: 1 or more.
    #[must_use]
    pub fn max_passes(&self) -> u16 {
        self.max_passes
    }

    /// The time that a stop waits for the steps in flight. Keep it below
    /// `TimeoutStopSec` of the unit.
    #[must_use]
    pub fn stop_grace(&self) -> Seconds {
        self.stop_grace
    }

    /// Whether the service acts or only prints its plan.
    #[must_use]
    pub fn mode(&self) -> Mode {
        self.mode
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A digest that no image has.
    const DIGEST: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fn image(name: &str) -> String {
        format!("registry.example/{name}@sha256:{DIGEST}")
    }

    /// The argument list of `systemd/creche-caregiver.service`, with the
    /// three variables of `managerd.env` in place.
    fn unit_args() -> RawServe {
        RawServe {
            registry: String::from("/srv/agents/registry"),
            image: image("playpen"),
            image_python: Some(image("playpen-python")),
            state_root: Some(String::from("/srv/agents/state/rework")),
            sessiond_socket: Some(String::from("/srv/agents/state/rework/sock/sessiond.sock")),
            pep_url: Some(String::from("http://192.0.2.10:8300")),
            write: true,
            ..RawServe::default()
        }
    }

    /// The variables that the service reads: the site file and
    /// `managerd.env`.
    fn unit_env() -> Env {
        Env::from_pairs([
            ("HOME", "/home/operator"),
            ("AGENT_LAN_ADDRESS", "192.0.2.10"),
            (MASTER_KEY, "sk-master-test"),
        ])
    }

    fn one_error(raw: &RawServe, env: &Env) -> ConfigError {
        let errors = CaregiverConfig::from_parts(raw, env).unwrap_err();

        assert_eq!(errors.as_slice().len(), 1, "{errors}");

        errors.as_slice()[0]
    }

    #[test]
    fn the_unit_gives_a_valid_config() {
        let config = CaregiverConfig::from_parts(&unit_args(), &unit_env()).unwrap();

        assert_eq!(config.registry().as_str(), "/srv/agents/registry");
        assert_eq!(config.state_root().as_str(), "/srv/agents/state/rework");
        assert_eq!(config.release_root().as_str(), "/var/lib/creche-handover");
        assert_eq!(config.images().base().as_str(), image("playpen"));
        assert_eq!(
            config.images().python().unwrap().as_str(),
            image("playpen-python")
        );
        assert_eq!(
            config.images().released().unwrap().as_str(),
            "/opt/components/playpen/images.env"
        );
        assert_eq!(config.litellm_url().as_str(), "http://192.0.2.10:4000");
        assert!(config.master_key().unwrap().matches(b"sk-master-test"));
        assert_eq!(
            config.attendance(),
            &AttendanceTarget::Socket(
                "/srv/agents/state/rework/sock/sessiond.sock"
                    .parse()
                    .unwrap()
            )
        );
        assert_eq!(
            config.pep_watch(),
            &PepWatch::On {
                url: "http://192.0.2.10:8300".parse().unwrap(),
                interval: Seconds::fixed(10.0).unwrap(),
                unreachable_after: Seconds::fixed(90.0).unwrap(),
            }
        );
        assert_eq!(config.poll_interval().as_secs_f64(), 2.0);
        assert_eq!(config.max_passes(), 4);
        assert_eq!(config.stop_grace().as_secs_f64(), 330.0);
        assert_eq!(config.mode(), Mode::Write);
    }

    #[test]
    fn a_flag_that_is_not_set_takes_the_plane_on_the_lan_address() {
        let raw = RawServe {
            registry: String::from("/srv/agents/registry"),
            image: image("playpen"),
            ..RawServe::default()
        };
        let config = CaregiverConfig::from_parts(&raw, &unit_env()).unwrap();

        assert_eq!(
            config.attendance(),
            &AttendanceTarget::Url("http://192.0.2.10:8350".parse().unwrap())
        );
        assert!(matches!(
            config.pep_watch(),
            PepWatch::On { url, .. } if url.as_str() == "http://192.0.2.10:8300"
        ));
        assert_eq!(config.images().python(), None);
        assert_eq!(config.mode(), Mode::Plan);
    }

    #[test]
    fn an_empty_pep_url_turns_the_watch_off() {
        let raw = RawServe {
            pep_url: Some(String::new()),
            ..unit_args()
        };
        let config = CaregiverConfig::from_parts(&raw, &unit_env()).unwrap();

        assert_eq!(config.pep_watch(), &PepWatch::Off);
    }

    #[test]
    fn each_flag_wins_over_its_default() {
        let raw = RawServe {
            state_root: Some(String::from("/tmp/root/state")),
            release_root: Some(String::from("/tmp/root/release")),
            litellm_base_url: Some(String::from("http://127.0.0.1:14000/")),
            sessiond_socket: None,
            sessiond_url: Some(String::from("http://127.0.0.1:18350")),
            pep_url: Some(String::from("http://127.0.0.1:18300")),
            pep_probe_interval_s: Some(String::from("0.2")),
            pep_unreachable_after_s: Some(String::from("1.5")),
            poll_interval_s: Some(String::from("0.1")),
            max_concurrent_passes: Some(String::from("1")),
            stop_grace_s: Some(String::from("3")),
            ..unit_args()
        };
        let env = Env::from_pairs([(MASTER_KEY, "sk-master-test")]);
        let config = CaregiverConfig::from_parts(&raw, &env).unwrap();

        assert_eq!(config.state_root().as_str(), "/tmp/root/state");
        assert_eq!(config.release_root().as_str(), "/tmp/root/release");
        assert_eq!(config.images().released(), None);
        assert_eq!(config.litellm_url().base(), "http://127.0.0.1:14000");
        assert_eq!(
            config.attendance(),
            &AttendanceTarget::Url("http://127.0.0.1:18350".parse().unwrap())
        );
        assert_eq!(
            config.pep_watch(),
            &PepWatch::On {
                url: "http://127.0.0.1:18300".parse().unwrap(),
                interval: Seconds::fixed(0.2).unwrap(),
                unreachable_after: Seconds::fixed(1.5).unwrap(),
            }
        );
        assert_eq!(config.poll_interval().as_secs_f64(), 0.1);
        assert_eq!(config.max_passes(), 1);
        assert_eq!(config.stop_grace().as_secs_f64(), 3.0);
    }

    #[test]
    fn a_plane_with_no_flag_needs_the_lan_address() {
        let env = Env::from_pairs([(MASTER_KEY, "sk-master-test")]);

        assert_eq!(
            one_error(&unit_args(), &env),
            ConfigError::Unset {
                variable: LAN_ADDRESS
            }
        );
    }

    #[test]
    fn a_state_root_of_its_own_needs_a_release_root_of_its_own() {
        let raw = RawServe {
            state_root: Some(String::from("/tmp/root/state")),
            ..unit_args()
        };

        assert_eq!(
            one_error(&raw, &unit_env()),
            ConfigError::BadForm {
                variable: "--release-root",
                form: "a release root of its own beside a state root of its own",
            }
        );
    }

    #[test]
    fn the_service_does_not_act_without_the_master_key() {
        let env = Env::from_pairs([("AGENT_LAN_ADDRESS", "192.0.2.10")]);
        let plan = RawServe {
            write: false,
            ..unit_args()
        };

        assert_eq!(
            one_error(&unit_args(), &env),
            ConfigError::Unset {
                variable: MASTER_KEY
            }
        );
        assert!(
            CaregiverConfig::from_parts(&plan, &env)
                .unwrap()
                .master_key()
                .is_none()
        );
    }

    #[test]
    fn an_image_is_a_reference_with_a_digest() {
        for text in [
            image("playpen"),
            image("team/play_pen.v2"),
            format!("localhost:5000/playpen@sha256:{DIGEST}"),
        ] {
            assert_eq!(text.parse::<ImageRef>().unwrap().as_str(), text);
        }

        for (text, error) in [
            (String::new(), ImageRefError::NoDigest),
            (String::from("playpen:latest"), ImageRefError::NoDigest),
            (
                String::from("registry.example/playpen:latest"),
                ImageRefError::NoDigest,
            ),
            (
                format!("registry.example/playpen@sha256:{}", DIGEST.to_uppercase()),
                ImageRefError::NoDigest,
            ),
            (
                format!("registry.example/playpen@sha256:{DIGEST}0"),
                ImageRefError::NoDigest,
            ),
            (
                format!("registry.example/playpen@sha256:{DIGEST}\n"),
                ImageRefError::NoDigest,
            ),
            (format!("playpen@sha256:{DIGEST}"), ImageRefError::BadName),
            (format!("/playpen@sha256:{DIGEST}"), ImageRefError::BadName),
            (
                format!("registry.example/@sha256:{DIGEST}"),
                ImageRefError::BadName,
            ),
            (
                format!("registry.example/-playpen@sha256:{DIGEST}"),
                ImageRefError::BadName,
            ),
            (
                format!("Registry.example/playpen@sha256:{DIGEST}"),
                ImageRefError::BadName,
            ),
        ] {
            assert_eq!(text.parse::<ImageRef>().unwrap_err(), error, "{text}");
        }
    }

    #[test]
    fn a_value_that_the_service_cannot_use_is_an_error_of_its_flag() {
        type Change = fn(&mut RawServe);
        let cases: [(&str, Change); 12] = [
            ("--registry", |raw| raw.registry = String::from("registry")),
            ("--image", |raw| raw.image = String::from("playpen:latest")),
            ("--image", |raw| raw.image = String::new()),
            ("--image-python", |raw| {
                raw.image_python = Some(String::from("python:3"));
            }),
            ("--released-images", |raw| {
                raw.released_images = Some(String::from("images.env"));
            }),
            ("--state-root", |raw| {
                raw.state_root = Some(String::from("state"))
            }),
            ("--sessiond-socket", |raw| {
                raw.sessiond_socket = Some(String::from("sessiond.sock"));
            }),
            ("--pep-url", |raw| {
                raw.pep_url = Some(String::from("192.0.2.10:8300"))
            }),
            ("--pep-probe-interval-s", |raw| {
                raw.pep_probe_interval_s = Some(String::from("nan"));
            }),
            ("--poll-interval-s", |raw| {
                raw.poll_interval_s = Some(String::from("0"));
            }),
            ("--max-concurrent-passes", |raw| {
                raw.max_concurrent_passes = Some(String::from("0"));
            }),
            ("--stop-grace-s", |raw| {
                raw.stop_grace_s = Some(String::from("soon"))
            }),
        ];
        for (flag_name, change) in cases {
            let mut raw = unit_args();
            change(&mut raw);

            assert_eq!(one_error(&raw, &unit_env()).variable(), flag_name);
        }
    }

    #[test]
    fn the_parse_collects_the_error_of_each_flag() {
        let raw = RawServe {
            registry: String::from("registry"),
            image: String::from("playpen"),
            poll_interval_s: Some(String::from("0")),
            ..unit_args()
        };
        let errors = CaregiverConfig::from_parts(&raw, &Env::from_pairs([("A", "b")])).unwrap_err();
        let names: Vec<&str> = errors
            .as_slice()
            .iter()
            .map(ConfigError::variable)
            .collect();

        assert_eq!(
            names,
            [
                "--registry",
                "--image",
                LAN_ADDRESS,
                MASTER_KEY,
                "--poll-interval-s"
            ]
        );
    }

    #[test]
    fn an_empty_flag_with_a_type_is_an_error_as_in_argparse() {
        // `argparse` gives the text to `float`, `int` or `Path`. The first
        // two refuse the empty text. `Path` reads it as `.`, which is not
        // absolute.
        type Change = fn(&mut RawServe);
        let cases: [(&str, Change); 9] = [
            ("--poll-interval-s", |raw| {
                raw.poll_interval_s = Some(String::new());
            }),
            ("--stop-grace-s", |raw| {
                raw.stop_grace_s = Some(String::new())
            }),
            ("--pep-probe-interval-s", |raw| {
                raw.pep_probe_interval_s = Some(String::new());
            }),
            ("--pep-unreachable-after-s", |raw| {
                raw.pep_unreachable_after_s = Some(String::new());
            }),
            ("--max-concurrent-passes", |raw| {
                raw.max_concurrent_passes = Some(String::new());
            }),
            ("--state-root", |raw| raw.state_root = Some(String::new())),
            ("--release-root", |raw| {
                raw.release_root = Some(String::new())
            }),
            ("--released-images", |raw| {
                raw.released_images = Some(String::new());
            }),
            ("--sessiond-socket", |raw| {
                raw.sessiond_socket = Some(String::new());
            }),
        ];
        for (flag_name, change) in cases {
            let mut raw = unit_args();
            change(&mut raw);

            assert_eq!(one_error(&raw, &unit_env()).variable(), flag_name);
        }
    }

    #[test]
    fn a_flag_is_read_with_no_strip_as_in_argparse() {
        type Change = fn(&mut RawServe);
        let cases: [(&str, Change); 6] = [
            ("--registry", |raw| {
                raw.registry = String::from(" /srv/agents/registry");
            }),
            ("--image", |raw| {
                raw.image = format!(" {}", image("playpen"))
            }),
            ("--image-python", |raw| {
                raw.image_python = Some(String::from(" "))
            }),
            ("--state-root", |raw| {
                raw.state_root = Some(String::from(" /srv/agents/state/rework"));
            }),
            ("--litellm-base-url", |raw| {
                raw.litellm_base_url = Some(String::from(" "));
            }),
            ("--pep-url", |raw| raw.pep_url = Some(String::from(" "))),
        ];
        for (flag_name, change) in cases {
            let mut raw = unit_args();
            change(&mut raw);

            assert_eq!(one_error(&raw, &unit_env()).variable(), flag_name);
        }

        // `float` and `int` remove the space themselves.
        let raw = RawServe {
            poll_interval_s: Some(String::from(" 2.5 ")),
            max_concurrent_passes: Some(String::from(" 3\n")),
            ..unit_args()
        };
        let config = CaregiverConfig::from_parts(&raw, &unit_env()).unwrap();

        assert_eq!(config.poll_interval().as_secs_f64(), 2.5);
        assert_eq!(config.max_passes(), 3);
    }

    #[test]
    fn three_flags_read_the_empty_text_as_not_set() {
        let raw = RawServe {
            image_python: Some(String::new()),
            litellm_base_url: Some(String::new()),
            sessiond_socket: None,
            sessiond_url: Some(String::new()),
            ..unit_args()
        };
        let config = CaregiverConfig::from_parts(&raw, &unit_env()).unwrap();

        assert_eq!(config.images().python(), None);
        assert_eq!(config.litellm_url().as_str(), "http://192.0.2.10:4000");
        assert_eq!(
            config.attendance(),
            &AttendanceTarget::Url("http://192.0.2.10:8350".parse().unwrap())
        );
    }

    #[test]
    fn a_watch_that_is_off_still_parses_its_two_times() {
        let raw = RawServe {
            pep_url: Some(String::new()),
            pep_probe_interval_s: Some(String::from("soon")),
            ..unit_args()
        };

        assert_eq!(
            one_error(&raw, &unit_env()).variable(),
            "--pep-probe-interval-s"
        );
    }

    #[test]
    fn the_master_key_is_the_exact_text_of_its_variable() {
        // The Python service reads the variable with no strip.
        let env = Env::from_pairs([(LAN_ADDRESS, "192.0.2.10"), (MASTER_KEY, " sk-master \n")]);
        let config = CaregiverConfig::from_parts(&unit_args(), &env).unwrap();

        assert!(config.master_key().unwrap().matches(b" sk-master \n"));

        let empty = Env::from_pairs([(LAN_ADDRESS, "192.0.2.10"), (MASTER_KEY, "")]);

        assert_eq!(
            one_error(&unit_args(), &empty),
            ConfigError::Unset {
                variable: MASTER_KEY
            }
        );
    }

    fn names(errors: &ConfigErrors) -> Vec<&'static str> {
        errors
            .as_slice()
            .iter()
            .map(ConfigError::variable)
            .collect()
    }

    #[test]
    fn a_lan_address_that_is_not_valid_does_not_hide_the_other_errors() {
        let raw = RawServe {
            registry: String::from("registry"),
            image: String::from("playpen"),
            poll_interval_s: Some(String::from("0")),
            pep_url: None,
            sessiond_socket: None,
            ..unit_args()
        };
        let env = Env::from_pairs([(LAN_ADDRESS, "0.0.0.0")]);
        let errors = CaregiverConfig::from_parts(&raw, &env).unwrap_err();

        assert_eq!(
            names(&errors),
            [
                "--registry",
                "--image",
                LAN_ADDRESS,
                MASTER_KEY,
                "--poll-interval-s"
            ]
        );
        assert!(matches!(errors.as_slice()[2], ConfigError::Address { .. }));
    }

    #[test]
    fn a_lan_address_that_three_planes_need_is_one_error() {
        let raw = RawServe {
            pep_url: None,
            sessiond_socket: None,
            ..unit_args()
        };
        let env = Env::from_pairs([(MASTER_KEY, "sk-master-test")]);

        assert_eq!(
            one_error(&raw, &env),
            ConfigError::Unset {
                variable: LAN_ADDRESS
            }
        );
    }

    #[test]
    fn a_lan_address_that_is_not_valid_is_an_error_with_a_flag_for_each_plane() {
        let raw = RawServe {
            litellm_base_url: Some(String::from("http://127.0.0.1:14000")),
            ..unit_args()
        };
        let env = Env::from_pairs([(LAN_ADDRESS, "0.0.0.0"), (MASTER_KEY, "sk-master-test")]);

        assert!(matches!(
            one_error(&raw, &env),
            ConfigError::Address {
                variable: LAN_ADDRESS,
                ..
            }
        ));
    }

    #[test]
    fn debug_prints_no_byte_of_the_master_key() {
        let config = CaregiverConfig::from_parts(&unit_args(), &unit_env()).unwrap();

        assert!(!format!("{config:?}").contains("sk-master-test"));
    }

    #[test]
    fn the_failure_action_is_an_exit_with_no_restart() {
        assert_eq!(CaregiverConfig::FAILURE.at_start(), AtStart::ExitConfig);
        assert_eq!(CaregiverConfig::FAILURE.at_reload(), AtReload::NotRead);
        assert_eq!(CaregiverConfig::UNIT, "creche-caregiver.service");
    }
}
