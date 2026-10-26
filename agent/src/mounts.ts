// A port of `playpen/src/mounts.ts`. sbx mounts a host directory at its own
// host path inside the VM, so no fixed in-VM path exists. The environment is
// the only source of each directory, and a missing one is a refusal.

/** The directories that each sandbox mounts, by the variable that names each one. */
const REQUIRED = {
  sessions: "AGENT_SESSIONS_DIR",
  creds: "AGENT_CRED_DIR",
  config: "AGENT_FAMILY_CONFIG_DIR",
  control: "AGENT_CONTROL_DIR",
} as const;

/** The `library/` index store. Only a family that mounts one has it. */
const INDEX_ROOT_ENV = "AGENT_INDEX_ROOT";

export type MountDirs = { readonly [Name in keyof typeof REQUIRED]: string } & {
  readonly indexRoot: string | null;
};

export type MountRead =
  | { readonly ok: true; readonly dirs: MountDirs }
  | { readonly ok: false; readonly unset: readonly string[] };

/** An empty value counts as an absent variable. */
export function readMountDirs(env: NodeJS.ProcessEnv): MountRead {
  const named = (variable: string): string => env[variable] ?? "";
  const unset = Object.values(REQUIRED).filter((variable) => named(variable).length === 0);
  if (unset.length > 0) {
    return { ok: false, unset };
  }

  const index = named(INDEX_ROOT_ENV);

  return {
    ok: true,
    dirs: {
      sessions: named(REQUIRED.sessions),
      creds: named(REQUIRED.creds),
      config: named(REQUIRED.config),
      control: named(REQUIRED.control),
      indexRoot: index.length > 0 ? index : null,
    },
  };
}
