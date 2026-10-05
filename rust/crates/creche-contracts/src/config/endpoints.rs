//! The address of another service: the names of five variables.
//!
//! A service takes each address of another service from its environment
//! (`rust/AGENTS.md`, "The rules for a service"). Each constant below is
//! the one home of the name of one such variable. The module of a daemon
//! reads the name from here and holds no copy of the text.
//!
//! Other variables hold an address too, for example the URL of `attendance`
//! that a door reads. Each of those names has its one home in the module of
//! the daemon that reads it.
//!
//! A plane is a service of the host that each sandbox calls: the chaperone
//! and LiteLLM (contract 01 §3.7 rule 4).

/// The variable that holds the URL of the chaperone. The chaperone is one of
/// the two planes.
pub const AGENT_PEP_URL: &str = "AGENT_PEP_URL";
/// The variable that holds the base URL of LiteLLM. LiteLLM is one of the
/// two planes.
pub const AGENT_LITELLM_BASE_URL: &str = "AGENT_LITELLM_BASE_URL";
/// The variable that holds the URL of TEI, the embeddings service. The name
/// has no `AGENT_` at its start: the Python package `library` reads this
/// name, and one value has one name.
pub const TEI_URL: &str = "TEI_URL";
/// The variable of the site file that holds the URL of Home Assistant.
pub const AGENT_HA_URL: &str = "AGENT_HA_URL";
/// The variable that holds the URL of the hook of an approval gate. The
/// chaperone calls that hook.
pub const PEP_APPROVAL_URL: &str = "PEP_APPROVAL_URL";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_name_is_the_text_that_the_services_read() {
        assert_eq!(AGENT_PEP_URL, "AGENT_PEP_URL");
        assert_eq!(AGENT_LITELLM_BASE_URL, "AGENT_LITELLM_BASE_URL");
        assert_eq!(TEI_URL, "TEI_URL");
        assert_eq!(AGENT_HA_URL, "AGENT_HA_URL");
        assert_eq!(PEP_APPROVAL_URL, "PEP_APPROVAL_URL");
    }
}
