//! Live or replacement, field by field (contract 01 §6, contract 05 §5.4).
//!
//! The function is pure: two parsed files in, one diff out. It reads no
//! clock, no disk and no host. The reconciler decides what to do from the
//! answer, and refuses a change to an immutable field with it.
//!
//! A [`Direction`] says what a change does to the reach of a family, not to
//! the text. A new `approval` entry narrows the reach, so its direction is
//! `remove`. Contract 05 §5.4 needs that reading: a revision that removes
//! reach interrupts, and a revision that only adds reach drains.

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};

use creche_contracts::family::{
    ALL_TOOLS, Mode, RawFamily, RawMount, RawToolGrant, RawTrigger, duration_s, memory_mb,
    python_float_text,
};

/// Makes an enum of texts with the text of each variant.
macro_rules! text_enum {
    (
        $(#[$attribute:meta])*
        $name:ident { $($variant:ident => $text:literal),+ $(,)? }
    ) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
        pub enum $name {
            $($variant,)+
        }

        impl $name {
            /// The text of the value.
            #[must_use]
            pub fn as_str(self) -> &'static str {
                match self {
                    $(Self::$variant => $text,)+
                }
            }
        }
    };
}

text_enum! {
    /// How a change to one field lands (contract 01 §6).
    ///
    /// The set is closed. A reader refuses another value.
    Landing {
        Live => "live",
        Replace => "replace",
        Immutable => "immutable",
    }
}

text_enum! {
    /// What a change does to the reach of a family (contract 05 §5.4).
    ///
    /// The set is closed. A reader refuses another value.
    Direction {
        Add => "add",
        Remove => "remove",
        Both => "both",
        Change => "change",
    }
}

text_enum! {
    /// The reconcile step of a change (contract 05 §3.4). `none` is for a
    /// field that lands through the status document, the timers or the
    /// trigger door.
    ///
    /// The set is closed. A reader refuses another value.
    Step {
        WriteGrants => "write_grants",
        UpdateKey => "update_key",
        WriteConfig => "write_config",
        SetEgress => "set_egress",
        CreateSandbox => "create_sandbox",
        None => "none",
    }
}

text_enum! {
    /// How a switch to a new sandbox treats the turns that run (contract 05
    /// §5.4).
    ///
    /// The set is closed. A reader refuses another value.
    SwitchMode {
        Drain => "drain",
        Interrupt => "interrupt",
    }
}

/// The change of one field.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FieldChange {
    pub field: &'static str,
    pub landing: Landing,
    pub direction: Direction,
    pub step: Step,
    pub detail: String,
}

/// Each change between two family files, in the order of the table of
/// contract 01 §6.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Diff {
    changes: Vec<FieldChange>,
}

impl Diff {
    #[must_use]
    pub fn changes(&self) -> &[FieldChange] {
        &self.changes
    }

    #[must_use]
    pub fn changed(&self) -> bool {
        !self.changes.is_empty()
    }

    /// The immutable fields that moved. A new `name` is a new family, and a
    /// new `kind` is refused (contract 01 §3.1).
    #[must_use]
    pub fn refused(&self) -> Vec<&FieldChange> {
        self.with_landing(Landing::Immutable)
    }

    #[must_use]
    pub fn needs_switch(&self) -> bool {
        !self.with_landing(Landing::Replace).is_empty()
    }

    /// Contract 05 §5.4. A revision that adds and removes is a removal.
    #[must_use]
    pub fn switch_mode(&self) -> Option<SwitchMode> {
        let replacing = self.with_landing(Landing::Replace);
        if replacing.is_empty() {
            return None;
        }

        let narrowing = replacing
            .iter()
            .any(|change| change.direction != Direction::Add);

        Some(if narrowing {
            SwitchMode::Interrupt
        } else {
            SwitchMode::Drain
        })
    }

    /// Each step one time, in the order of the fields.
    #[must_use]
    pub fn steps(&self) -> Vec<Step> {
        let mut ordered = Vec::new();
        for change in &self.changes {
            if change.step != Step::None && !ordered.contains(&change.step) {
                ordered.push(change.step);
            }
        }

        ordered
    }

    /// One line for the `reason` of the switch call (contract 05 §5.1).
    #[must_use]
    pub fn reason(&self) -> String {
        if self.changes.is_empty() {
            return "no change".to_owned();
        }

        self.changes
            .iter()
            .map(|change| change.detail.as_str())
            .collect::<Vec<_>>()
            .join("; ")
    }

    fn with_landing(&self, landing: Landing) -> Vec<&FieldChange> {
        self.changes
            .iter()
            .filter(|change| change.landing == landing)
            .collect()
    }

    fn add(
        &mut self,
        field: &'static str,
        landing: Landing,
        direction: Direction,
        step: Step,
        detail: String,
    ) {
        self.changes.push(FieldChange {
            field,
            landing,
            direction,
            step,
            detail,
        });
    }

    fn live(&mut self, field: &'static str, direction: Direction, step: Step, detail: String) {
        self.add(field, Landing::Live, direction, step, detail);
    }

    fn replace(&mut self, field: &'static str, direction: Direction, detail: String) {
        self.add(
            field,
            Landing::Replace,
            direction,
            Step::CreateSandbox,
            detail,
        );
    }
}

/// The direction of a number that moved. A number that the file does not
/// hold has no order.
fn number<T: PartialOrd>(old: Option<T>, new: Option<T>) -> Direction {
    match (old, new) {
        (Some(old), Some(new)) => match new.partial_cmp(&old) {
            Some(Ordering::Greater) => Direction::Add,
            Some(Ordering::Less) => Direction::Remove,
            _ => Direction::Change,
        },
        _ => Direction::Change,
    }
}

fn flip(direction: Direction) -> Direction {
    match direction {
        Direction::Add => Direction::Remove,
        Direction::Remove => Direction::Add,
        other => other,
    }
}

fn from_sets<T: Ord>(before: &BTreeSet<T>, after: &BTreeSet<T>) -> Direction {
    let added = after.difference(before).next().is_some();
    let removed = before.difference(after).next().is_some();
    match (added, removed) {
        (true, true) => Direction::Both,
        (true, false) => Direction::Add,
        _ => Direction::Remove,
    }
}

/// `None` when the two lists hold the same members. The order never
/// matters: a list here is a set that the file writes.
fn members(old: &[String], new: &[String]) -> Option<Direction> {
    let before: BTreeSet<&str> = old.iter().map(String::as_str).collect();
    let after: BTreeSet<&str> = new.iter().map(String::as_str).collect();

    (before != after).then(|| from_sets(&before, &after))
}

fn grant_direction(old: &RawToolGrant, new: &RawToolGrant) -> Option<Direction> {
    if old == new {
        return None;
    }

    match (old, new) {
        (RawToolGrant::Named(old), RawToolGrant::Named(new)) => members(old, new),
        // `all` takes its tools from the server file at read time, so a list
        // has no order against it. A change to `all` adds. A change from it
        // can add and remove.
        (_, RawToolGrant::Text(text)) if text == ALL_TOOLS => Some(Direction::Add),
        _ => Some(Direction::Both),
    }
}

fn tools_direction(old: &RawFamily, new: &RawFamily) -> Option<Direction> {
    if old.tools == new.tools {
        return None;
    }

    let before: BTreeSet<&String> = old.tools.keys().collect();
    let after: BTreeSet<&String> = new.tools.keys().collect();
    let mut moves = BTreeSet::new();
    if before != after {
        moves.insert(from_sets(&before, &after));
    }

    for (server, grant) in &old.tools {
        let moved = new
            .tools
            .get(server)
            .and_then(|new_grant| grant_direction(grant, new_grant));
        moves.extend(moved);
    }

    let mut moves = moves.into_iter();
    match (moves.next(), moves.next()) {
        (None, _) => None,
        (Some(one), None) => Some(one),
        _ => Some(Direction::Both),
    }
}

fn verbs_direction(old: &RawFamily, new: &RawFamily) -> Direction {
    let before: BTreeSet<_> = old.verbs.granted().into_iter().collect();
    let after: BTreeSet<_> = new.verbs.granted().into_iter().collect();
    if before != after {
        return from_sets(&before, &after);
    }

    // The same verbs with another fence. A fence widens and narrows.
    Direction::Both
}

/// The members of a list of triggers, each one time.
fn trigger_set(triggers: Option<&Vec<RawTrigger>>) -> Vec<&RawTrigger> {
    let mut set: Vec<&RawTrigger> = Vec::new();
    for trigger in triggers.into_iter().flatten() {
        if !set.contains(&trigger) {
            set.push(trigger);
        }
    }

    set
}

fn triggers_direction(old: &RawFamily, new: &RawFamily) -> Direction {
    let before = trigger_set(old.triggers.as_ref());
    let after = trigger_set(new.triggers.as_ref());
    let added = after.iter().any(|trigger| !before.contains(trigger));
    let removed = before.iter().any(|trigger| !after.contains(trigger));
    match (added, removed) {
        (false, false) => Direction::Change,
        (true, true) => Direction::Both,
        (true, false) => Direction::Add,
        (false, true) => Direction::Remove,
    }
}

/// The mode of each mounted path. For a path that the file mounts twice,
/// the last mode stays.
fn mount_modes(files: &[RawMount]) -> BTreeMap<&str, &str> {
    files
        .iter()
        .map(|mount| (mount.path.as_str(), mount.mode.as_str()))
        .collect()
}

fn mounts_direction(old: &[RawMount], new: &[RawMount]) -> Option<Direction> {
    let before = mount_modes(old);
    let after = mount_modes(new);
    if before == after {
        return None;
    }

    let mut added = after.keys().any(|path| !before.contains_key(path));
    let mut removed = before.keys().any(|path| !after.contains_key(path));
    for (path, mode) in &after {
        match before.get(path) {
            Some(old_mode) if old_mode == mode => {}
            // `rw` to `ro` removes reach. `ro` to `rw` adds reach.
            Some(_) if *mode == Mode::Ro.as_str() => removed = true,
            Some(_) => added = true,
            None => {}
        }
    }

    Some(match (added, removed) {
        (true, true) => Direction::Both,
        (true, false) => Direction::Add,
        _ => Direction::Remove,
    })
}

fn mounts_detail(old: &[RawMount], new: &[RawMount]) -> String {
    let before = mount_modes(old);
    let after = mount_modes(new);
    let mut parts = Vec::new();
    for (path, mode) in &after {
        if !before.contains_key(path) {
            parts.push(format!("added {path} {mode}"));
        }
    }

    for (path, mode) in &before {
        if !after.contains_key(path) {
            parts.push(format!("removed {path} {mode}"));
        }
    }

    for (path, mode) in &before {
        if let Some(new_mode) = after.get(path).filter(|new_mode| *new_mode != mode) {
            parts.push(format!("{path} {mode} to {new_mode}"));
        }
    }

    format!("mounts changed: {}", parts.join(", "))
}

/// The text that Python writes for a value that the file can omit.
fn or_none<T: ToString>(value: Option<&T>) -> String {
    value.map_or_else(|| "None".to_owned(), ToString::to_string)
}

/// The text that Python writes for a boolean.
fn python_bool(value: bool) -> &'static str {
    if value { "True" } else { "False" }
}

fn immutables(old: &RawFamily, new: &RawFamily, diff: &mut Diff) {
    if old.name != new.name {
        diff.add(
            "name",
            Landing::Immutable,
            Direction::Change,
            Step::None,
            format!(
                "name changed: '{}' to '{}' is a new family, and the old one is deleted \
                 credentials first",
                old.name, new.name
            ),
        );
    }

    if old.kind != new.kind {
        diff.add(
            "kind",
            Landing::Immutable,
            Direction::Change,
            Step::None,
            format!(
                "kind changed: '{}' to '{}' is refused; live sessions were created under the old \
                 rule",
                old.kind, new.kind
            ),
        );
    }

    if old.description != new.description {
        diff.live(
            "description",
            Direction::Change,
            Step::None,
            "description changed: registry only".to_owned(),
        );
    }
}

fn model(old: &RawFamily, new: &RawFamily, diff: &mut Diff) {
    if old.model.router != new.model.router {
        diff.live(
            "model.router",
            Direction::Both,
            Step::UpdateKey,
            format!(
                "model.router changed: '{}' to '{}'",
                old.model.router, new.model.router
            ),
        );
    }

    let (old_budget, new_budget) = (old.model.budget_usd_per_day, new.model.budget_usd_per_day);
    if old_budget != new_budget {
        diff.live(
            "model.budget_usd_per_day",
            number(Some(old_budget), Some(new_budget)),
            Step::UpdateKey,
            format!(
                "model.budget_usd_per_day changed: {} to {}",
                python_float_text(old_budget),
                python_float_text(new_budget)
            ),
        );
    }
}

fn grants(old: &RawFamily, new: &RawFamily, diff: &mut Diff) {
    if let Some(direction) = tools_direction(old, new) {
        diff.live(
            "tools",
            direction,
            Step::WriteGrants,
            format!(
                "tools changed ({}): the PEP re-reads the grant file per call",
                direction.as_str()
            ),
        );
    }

    if old.verbs != new.verbs {
        diff.live(
            "verbs",
            verbs_direction(old, new),
            Step::WriteGrants,
            "verbs changed".to_owned(),
        );
    }

    if let Some(direction) = members(&old.delegates, &new.delegates) {
        diff.live(
            "delegates",
            direction,
            Step::WriteGrants,
            "delegates changed".to_owned(),
        );
    }

    let (old_cap, new_cap) = (&old.max_inflight_delegations, &new.max_inflight_delegations);
    if old_cap != new_cap {
        diff.live(
            "max_inflight_delegations",
            number(Some(old_cap), Some(new_cap)),
            Step::WriteGrants,
            format!("max_inflight_delegations changed: {old_cap} to {new_cap}"),
        );
    }

    if let Some(direction) = members(&old.approval, &new.approval) {
        // An approval entry only narrows, so a new entry removes reach.
        diff.live(
            "approval",
            flip(direction),
            Step::WriteGrants,
            "approval changed: an added entry narrows".to_owned(),
        );
    }
}

fn config(old: &RawFamily, new: &RawFamily, diff: &mut Diff) {
    if let Some(direction) = members(&old.egress, &new.egress) {
        diff.live(
            "egress",
            direction,
            Step::SetEgress,
            "egress changed: a policy change on the running sandbox".to_owned(),
        );
    }

    if let Some(direction) = members(&old.skills, &new.skills) {
        diff.live(
            "skills",
            direction,
            Step::WriteConfig,
            "skills changed: read at turn start".to_owned(),
        );
    }

    if old.shell != new.shell {
        let direction = if new.shell {
            Direction::Add
        } else {
            Direction::Remove
        };
        diff.live(
            "shell",
            direction,
            Step::WriteConfig,
            format!(
                "shell changed: {} to {}, read when a pi process next starts",
                python_bool(old.shell),
                python_bool(new.shell)
            ),
        );
    }

    if let Some(direction) = members(&old.sandbox_tools, &new.sandbox_tools) {
        diff.live(
            "sandbox_tools",
            direction,
            Step::WriteConfig,
            "sandbox_tools changed: read when a pi process next starts".to_owned(),
        );
    }

    if old.system_prompt != new.system_prompt {
        diff.live(
            "system_prompt",
            Direction::Change,
            Step::WriteConfig,
            format!(
                "system_prompt changed: {} to {}, read when a pi process next starts",
                old.system_prompt, new.system_prompt
            ),
        );
    }
}

fn kind_fields(old: &RawFamily, new: &RawFamily, diff: &mut Diff) {
    let old_timeout = old.job.as_ref().map(|job| &job.timeout);
    let new_timeout = new.job.as_ref().map(|job| &job.timeout);
    if old_timeout != new_timeout {
        let seconds = |timeout: Option<&String>| timeout.and_then(|text| duration_s(text));
        diff.live(
            "job.timeout",
            number(seconds(old_timeout), seconds(new_timeout)),
            Step::None,
            format!(
                "job.timeout changed: {} to {}, read per job",
                or_none(old_timeout),
                or_none(new_timeout)
            ),
        );
    }

    if old.triggers != new.triggers {
        diff.live(
            "triggers",
            triggers_direction(old, new),
            Step::None,
            "triggers changed: the timer set is rewritten".to_owned(),
        );
    }

    let (old_turns, new_turns) = (
        old.max_running_turns.as_ref(),
        new.max_running_turns.as_ref(),
    );
    if old_turns != new_turns {
        diff.live(
            "max_running_turns",
            number(old_turns, new_turns),
            Step::None,
            format!(
                "max_running_turns changed: {} to {}",
                or_none(old_turns),
                or_none(new_turns)
            ),
        );
    }

    // `change`: the check grants and removes no reach. It only decides
    // whether a cron trigger wakes the family (contract 01 §3.15).
    if old.quiet != new.quiet {
        diff.live(
            "quiet",
            Direction::Change,
            Step::None,
            "quiet changed: read at the next cron firing".to_owned(),
        );
    }
}

fn sandbox(old: &RawFamily, new: &RawFamily, diff: &mut Diff) {
    if let Some(direction) = mounts_direction(&old.files, &new.files) {
        diff.replace("files", direction, mounts_detail(&old.files, &new.files));
    }

    let (old_box, new_box) = (&old.sandbox, &new.sandbox);
    if old_box.image != new_box.image {
        // `change`: the flavors have no order, so the switch interrupts.
        diff.replace(
            "sandbox.image",
            Direction::Change,
            format!(
                "sandbox.image changed: '{}' to '{}'",
                old_box.image, new_box.image
            ),
        );
    }

    if old_box.cpus != new_box.cpus {
        diff.replace(
            "sandbox.cpus",
            number(Some(&old_box.cpus), Some(&new_box.cpus)),
            format!("sandbox.cpus changed: {} to {}", old_box.cpus, new_box.cpus),
        );
    }

    if old_box.memory != new_box.memory {
        diff.replace(
            "sandbox.memory",
            number(memory_mb(&old_box.memory), memory_mb(&new_box.memory)),
            format!(
                "sandbox.memory changed: {} to {}",
                old_box.memory, new_box.memory
            ),
        );
    }

    let (old_resident, new_resident) = (
        &old_box.max_resident_processes,
        &new_box.max_resident_processes,
    );
    if old_resident != new_resident {
        diff.replace(
            "sandbox.max_resident_processes",
            number(Some(old_resident), Some(new_resident)),
            format!("sandbox.max_resident_processes changed: {old_resident} to {new_resident}"),
        );
    }
}

/// Each field of the table of contract 01 §6, in the order of that table:
/// the answer of the Python `classify`.
///
/// The files are raw: the Python function takes each file that has the
/// right shape. For two valid families, give `RawFamily::from` of each.
#[must_use]
pub fn classify(old: &RawFamily, new: &RawFamily) -> Diff {
    let mut diff = Diff::default();
    immutables(old, new, &mut diff);
    model(old, new, &mut diff);
    grants(old, new, &mut diff);
    config(old, new, &mut diff);
    kind_fields(old, new, &mut diff);
    sandbox(old, new, &mut diff);

    diff
}
