//! The egress of a VM (decisions CH6-1, CH6-3, CH6-4). `rule` holds the
//! one decision of a target. The route `check` answers with it today, and
//! the CONNECT proxy of host port 1104 uses the same function (decision
//! CH69 rule 2).

pub mod rule;
