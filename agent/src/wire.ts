// The JSON Schema of the wire types, a copy of rust/crates/wire/schema/wire.json
// (decision CH22). The schema test of rust/crates/wire writes this file. Never
// edit it: run `CRECHE_WIRE_SCHEMA=write cargo test -p creche-wire` in rust/.

export const wire = {
  "$defs": {
    "Auto": {
      "additionalProperties": false,
      "description": "The origin of a session that attendance opened for one autonomous job.",
      "properties": {
        "job": {
          "$ref": "#/$defs/AutoJobId",
          "description": "The id that attendance gives the job. The `SessionId` of the row is a\nseparate value."
        },
        "trigger": {
          "$ref": "#/$defs/Trigger",
          "description": "What fired the job."
        }
      },
      "required": [
        "job",
        "trigger"
      ],
      "type": "object"
    },
    "AutoJobId": {
      "description": "The id that attendance gives an autonomous job of a trigger or of the\ndispatch door: `auto-<ulid>` (contract 02 §2 and §13). Contract 02 calls\nit a session id. The `SessionId` of the job's row on the host session\nlist is a separate value.",
      "pattern": "^auto-[0-9A-HJKMNP-TV-Z]{26}$",
      "type": "string"
    },
    "ConfigMissing": {
      "additionalProperties": false,
      "description": "The reply of a pull when `config/` is absent. It carries nothing.",
      "type": "object"
    },
    "ControlHello": {
      "additionalProperties": false,
      "description": "What the vmm sends first on each new control link, guest port 1200\n(decisions CH13, CH14). A new link replaces the old one (decision CH19\nrule 2). Guest init sets `CLOCK_REALTIME` to `sent_at` with\n`clock_settime` before it answers with `ControlReady` (decision CH28),\nbecause after a restore the guest clock is behind by the time the VM\nspent as a snapshot (fact 73).\n\nThe hello carries no family name and no protocol version (decision CH32\nrules 1 and 2). The vmm writes guest init at each cold boot (decision\nCH7-6). A restore brings back the guest init of the snapshot, so the\nvmm restores a snapshot only when its key holds the same guest-init\nprotocol version (decision CH7-6). So the two never skew in protocol.\nA later hello can name a session that differs from the first one. Guest\ninit then closes the link, and the vmm records a fault and cold-boots\nthe VM (decision CH32 rule 3). No wire type carries that case.",
      "properties": {
        "sent_at": {
          "$ref": "#/$defs/UnixMillis",
          "description": "The wall time of the vmm when it sent the hello."
        },
        "session": {
          "$ref": "#/$defs/SessionId",
          "description": "The session that the VM serves (decisions CH1, CH5e). The facade\nopens this one session at start, and creates it when it is new\n(decisions CH21-8, CH25 rule 2)."
        }
      },
      "required": [
        "sent_at",
        "session"
      ],
      "type": "object"
    },
    "ControlReady": {
      "additionalProperties": false,
      "description": "What guest init answers to `ControlHello`, after it sets its clock\n(decisions CH28, CH32). It holds the facade state (decision CH19 rule 2).\nThe last turn end sits in the facade state. The answer stays an object,\nso a later field has a place.",
      "properties": {
        "facade": {
          "$ref": "#/$defs/FacadeState",
          "description": "The facade."
        }
      },
      "required": [
        "facade"
      ],
      "type": "object"
    },
    "CreateSession": {
      "additionalProperties": false,
      "description": "What attendance sends to the vmm to make a row of the host session list\n(decisions CH21-4, CH25 rule 2). It is a `SessionRow` without `title`\nand `updated_at`. The vmm sets `title` to `null` and `updated_at` to\n`created_at`. It makes no session disk at create (decision CH21-4).\n\nTwo creates are the same when their typed values are equal\n(`PartialEq`), not when their bytes are equal. A client retries after\nENOENT, ECONNREFUSED or EOF (decision CH20). attendance stores the create\nbefore it sends it the first time. A retry resends the same `created_at`,\nso the repeat is the same create.",
      "properties": {
        "created_at": {
          "$ref": "#/$defs/UnixMillis",
          "description": "When attendance created the session."
        },
        "cwd": {
          "$ref": "#/$defs/SessionCwd",
          "description": "The `cwd` that the client sent in `session/new`, or that attendance\nchose for a job."
        },
        "family": {
          "$ref": "#/$defs/FamilyName",
          "description": "The family that serves the session."
        },
        "id": {
          "$ref": "#/$defs/SessionId",
          "description": "The id that attendance chose (decision CH25 rule 2)."
        },
        "origin": {
          "$ref": "#/$defs/SessionOrigin",
          "description": "Who made the session."
        }
      },
      "required": [
        "created_at",
        "cwd",
        "family",
        "id",
        "origin"
      ],
      "type": "object"
    },
    "CredsMissing": {
      "additionalProperties": false,
      "description": "The reply of a pull when `creds.json` is absent. It carries nothing.\nWhen `config/` is absent too, this reply wins.",
      "type": "object"
    },
    "Delegate": {
      "additionalProperties": false,
      "description": "The origin of a session that the delegate door opened for one thin job.\n\nIt is a type of its own so that the generated Python class has a name\nof its own. An inline struct variant gets the class name of its key.",
      "properties": {
        "delegation": {
          "$ref": "#/$defs/DelegationId",
          "description": "The delegation that asked for the job."
        },
        "job": {
          "$ref": "#/$defs/JobId",
          "description": "The id that attendance gives the job. The `SessionId` of the row is a\nseparate value."
        }
      },
      "required": [
        "delegation",
        "job"
      ],
      "type": "object"
    },
    "DelegationId": {
      "description": "The id the PEP mints for one delegation, a ULID (contract 04 §7.4).",
      "pattern": "^[0-9A-HJKMNP-TV-Z]{26}$",
      "type": "string"
    },
    "Dispatch": {
      "additionalProperties": false,
      "description": "The trigger of a dispatch: the `enqueue` verb of another family started\nthe job (contract 01 §3.13, contract 02 §13.4).",
      "properties": {
        "chain": {
          "$ref": "#/$defs/DispatchChain",
          "description": "The delegation chain that reached the job. attendance today stores a\ndispatch with no chain when `POST /dispatch` carries none, so it must\nrefuse a missing or empty chain before it writes this origin."
        },
        "name": {
          "$ref": "#/$defs/FamilyName",
          "description": "The calling family. attendance takes it from the family token, never\nfrom the request body (contract 02 §13.2 rule 5)."
        }
      },
      "required": [
        "chain",
        "name"
      ],
      "type": "object"
    },
    "DispatchChain": {
      "description": "The delegation chain that reached a dispatched job, caller first and the\njob's family last (contract 02 §13.2 rule 6). It holds 1 to 8 family\nnames. The PEP builds it from the delegations it minted (contract 04\n§6.3).",
      "items": {
        "$ref": "#/$defs/FamilyName"
      },
      "maxItems": 8,
      "minItems": 1,
      "type": "array"
    },
    "Door": {
      "additionalProperties": false,
      "description": "The origin of a session that a door client opened. It carries nothing.\n\nIt is a type of its own so that the generated Python model is a class\nwith no field. An inline empty object generates a `dict` with lax keys.",
      "type": "object"
    },
    "FacadeExited": {
      "additionalProperties": false,
      "description": "A facade that exited: `waitpid` reported it gone, and guest init did not\nstart the next one yet (decision CH19 rule 1). No turn runs.\n\nEach key is required. `last_turn_end` reads with `Option::deserialize`,\nso a missing key is refused.",
      "properties": {
        "last_turn_end": {
          "anyOf": [
            {
              "$ref": "#/$defs/TurnEnd"
            },
            {
              "type": "null"
            }
          ],
          "description": "The last turn end that guest init got since the VM booted, or `null`\nwhen no turn ended. `FacadeState` says how the vmm applies it."
        },
        "starts": {
          "$ref": "#/$defs/StartCount",
          "description": "How many times guest init started the facade."
        }
      },
      "required": [
        "last_turn_end",
        "starts"
      ],
      "type": "object"
    },
    "FacadeNotStarted": {
      "additionalProperties": false,
      "description": "The facade before guest init starts it the first time. It carries\nnothing: no turn can end before a facade starts.",
      "type": "object"
    },
    "FacadeRunning": {
      "additionalProperties": false,
      "description": "A facade process that runs and opened its local link to guest init.\n\nEach key is required. `last_turn_end` reads with `Option::deserialize`,\nso a missing key is refused.",
      "properties": {
        "last_turn_end": {
          "anyOf": [
            {
              "$ref": "#/$defs/TurnEnd"
            },
            {
              "type": "null"
            }
          ],
          "description": "The last turn end that guest init got since the VM booted, or `null`\nwhen no turn ended. `FacadeState` says how the vmm applies it."
        },
        "pid": {
          "description": "The pid of the facade in the guest. Guest init is pid 1, and a\n64-bit Linux keeps each pid below 2^22, so the facade's pid is 2 to\n2^22 - 1. The type accepts 1 to 2^32 - 1, the range of `Child::id`.",
          "format": "uint32",
          "maximum": 4294967295,
          "minimum": 1,
          "type": "integer"
        },
        "starts": {
          "$ref": "#/$defs/StartCount",
          "description": "How many times guest init started the facade, this start too."
        },
        "turn_running": {
          "description": "True when the facade sent a turn start and no turn end after it\n(decision CH21).",
          "type": "boolean"
        }
      },
      "required": [
        "last_turn_end",
        "pid",
        "starts",
        "turn_running"
      ],
      "type": "object"
    },
    "FacadeStarting": {
      "additionalProperties": false,
      "description": "A facade process that guest init started, which did not open its local\nlink to guest init yet (decision CH32 rule 6). Guest init gets no turn\nevent before that link opens, so this state has no turn flag.\n\nEach key is required. `last_turn_end` reads with `Option::deserialize`,\nso a missing key is refused.",
      "properties": {
        "last_turn_end": {
          "anyOf": [
            {
              "$ref": "#/$defs/TurnEnd"
            },
            {
              "type": "null"
            }
          ],
          "description": "The last turn end that guest init got since the VM booted, or `null`\nwhen no turn ended. A facade that exited and starts again keeps it,\nbecause guest init keeps it. On the first start (`starts` 1) it is\nalways `null`, because no local link opened yet (decision CH32 rule\n7). The type does not refuse a value there. `FacadeState` says how\nthe vmm applies it."
        },
        "pid": {
          "description": "The pid of the facade in the guest, as in `FacadeRunning`.",
          "format": "uint32",
          "maximum": 4294967295,
          "minimum": 1,
          "type": "integer"
        },
        "starts": {
          "$ref": "#/$defs/StartCount",
          "description": "How many times guest init started the facade, this start too."
        }
      },
      "required": [
        "last_turn_end",
        "pid",
        "starts"
      ],
      "type": "object"
    },
    "FacadeState": {
      "description": "The facade as guest init sees it (decision CH19 rule 2). Only a live\nfacade has a pid, only a running facade has a turn, and a facade that\nnever started has no start count.\n\nThe facade is up for decision CH19 rule 6 only in `running`. So the vmm\nsnapshots a VM only when the facade is `running` and `turn_running` is\nfalse. A `starting` facade is not up: guest init cannot see its turns\nyet, so the vmm never snapshots a turn that guest init cannot see\n(decision CH32 rule 6). When the local link of a live facade closes,\nguest init kills the facade, and the state moves to `exited` after\n`waitpid` (decision CH32 rule 7). So a facade never goes back from\n`running` to `starting`.\n\nEach state after the first start holds `last_turn_end`, so\n`not_started` cannot carry one (decision CH32 rule 5). A restore can\ndrop a turn event on the old link (fact 45), so guest init reports the\nlast `TurnEnd`, not a current title (decision CH32 rule 4). The vmm\napplies it as it applies `TurnEvent::End`: it sets the row's\n`updated_at`, and it sets the row's title only while that is `null`.\nSo an end that a restore dropped still arrives.\n\nOn the wire the variant name is the one key of an object, and each\nvariant has a `title` for its class name.",
      "oneOf": [
        {
          "additionalProperties": false,
          "description": "Guest init did not start the facade yet.",
          "properties": {
            "not_started": {
              "$ref": "#/$defs/FacadeNotStarted"
            }
          },
          "required": [
            "not_started"
          ],
          "title": "FacadeNotStartedState",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The facade process is alive and did not open its local link yet.",
          "properties": {
            "starting": {
              "$ref": "#/$defs/FacadeStarting"
            }
          },
          "required": [
            "starting"
          ],
          "title": "FacadeStartingState",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The facade runs and opened its local link.",
          "properties": {
            "running": {
              "$ref": "#/$defs/FacadeRunning"
            }
          },
          "required": [
            "running"
          ],
          "title": "FacadeRunningState",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The facade exited, and the next one did not start yet.",
          "properties": {
            "exited": {
              "$ref": "#/$defs/FacadeExited"
            }
          },
          "required": [
            "exited"
          ],
          "title": "FacadeExitedState",
          "type": "object"
        }
      ]
    },
    "FamilyConfig": {
      "additionalProperties": false,
      "description": "The family's part of `config/` that the guest reads: what caregiver writes\nin `write_config` (`steps.py:229-249`). Instructions, skills, sandbox\ntools and the model change at the next prompt of a running session\n(decision CH5c).\n\n`shell` of `runtime.json` is not here: the agent reads it and grants\nnothing with it (`agent/src/runtime-config.ts:18`).\n\n`model_live_at` of `runtime.json` is not here either. The vmm holds the\nreply on it (`FamilyStateReply`).",
      "properties": {
        "instructions": {
          "description": "The text of `config/instructions.md`. The agent trims it, and an empty\ntext gives no instructions, as today (`agent/src/agent.ts:72-85`).",
          "type": "string"
        },
        "model_alias": {
          "$ref": "#/$defs/ModelAlias",
          "description": "The alias of the model."
        },
        "sandbox_tools": {
          "$ref": "#/$defs/SandboxGrant",
          "description": "The granted sandbox tools."
        },
        "skills": {
          "$ref": "#/$defs/FamilySkills",
          "description": "The granted skills."
        },
        "system_prompt": {
          "description": "How the instructions meet the agent's own prompt. caregiver leaves\n`system_prompt` out of `runtime.json` for `append`\n(`config_mount.py:54`), so the vmm reads a missing key as `append`.",
          "enum": [
            "append",
            "replace"
          ],
          "title": "SystemPrompt",
          "type": "string"
        }
      },
      "required": [
        "instructions",
        "model_alias",
        "sandbox_tools",
        "skills",
        "system_prompt"
      ],
      "type": "object"
    },
    "FamilyCreds": {
      "additionalProperties": false,
      "description": "The family's part of `creds.json` that the guest reads (decision CH23\ndefault 1). `epoch`, `written_at`, `previous_pep_token` and\n`previous_expires_at` stay on the host, so the old token never sits in\nguest memory or in a snapshot.",
      "properties": {
        "litellm_key": {
          "$ref": "#/$defs/LitellmKey",
          "description": "The key for the LiteLLM plane."
        },
        "pep_token": {
          "$ref": "#/$defs/PepToken",
          "description": "The token for the chaperone plane."
        }
      },
      "required": [
        "litellm_key",
        "pep_token"
      ],
      "type": "object"
    },
    "FamilyName": {
      "description": "The name of a family: `FAMILY_NAME` of `family/grammar.py`.",
      "pattern": "^[a-z][a-z0-9-]{1,30}$",
      "type": "string"
    },
    "FamilySkills": {
      "additionalProperties": {
        "type": "string"
      },
      "description": "The granted skills: each name, and the text of its `SKILL.md`.",
      "propertyNames": {
        "$ref": "#/$defs/SkillName"
      },
      "type": "object"
    },
    "FamilyState": {
      "additionalProperties": false,
      "description": "The whole family state of one pull: the full typed state, every time\n(decision CH23). The facade compares the fields itself.",
      "properties": {
        "config": {
          "$ref": "#/$defs/FamilyConfig",
          "description": "What the vmm read from `config/`."
        },
        "creds": {
          "$ref": "#/$defs/FamilyCreds",
          "description": "What the vmm read from `creds.json`."
        }
      },
      "required": [
        "config",
        "creds"
      ],
      "type": "object"
    },
    "FamilyStateReply": {
      "description": "What the vmm answers on host port 1100 when the facade pulls the family\nstate, before each prompt (decisions CH5a, CH5b, CH23). The vmm reads\ncaregiver's `creds.json` and `config/` at each pull.\n\nA read failure is a typed reply, and the link stays open (decision CH23\ndefault 2). The facade fails that prompt and keeps running. At start it\npulls again with a backoff, and never exits on a typed error. When\n`creds.json` and `config/` are both absent, `creds_missing` wins\n(decision CH31).\n\nA reply text has a cap of 256 KiB (decision CH23 default 4). The cap is\na rule of the read surface, not of this type: the vmm's writer and the\n`cap` of `creche_wire.read` hold it, because JSON Schema counts code\npoints, not bytes. A `state` reply over the cap is `too_large`, not\n`not_valid` (decision CH31).\n\nThe vmm reads `model_live_at` from `config/runtime.json` (decision\nCH23-1). It holds the `state` reply until the host time passes that\ntime, for at most 10 s (decision CH31). No wire type carries\n`model_live_at`, and the guest needs no clock for it. A held pull can\ntake up to 10 s longer.\n\nOn the wire the variant name is the one key of an object, and each\nvariant has a `title` for its class name.",
      "oneOf": [
        {
          "additionalProperties": false,
          "description": "The vmm read the whole state.",
          "properties": {
            "state": {
              "$ref": "#/$defs/FamilyState"
            }
          },
          "required": [
            "state"
          ],
          "title": "StateReply",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "`creds.json` is absent.",
          "properties": {
            "creds_missing": {
              "$ref": "#/$defs/CredsMissing"
            }
          },
          "required": [
            "creds_missing"
          ],
          "title": "CredsMissingReply",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "`config/` is absent.",
          "properties": {
            "config_missing": {
              "$ref": "#/$defs/ConfigMissing"
            }
          },
          "required": [
            "config_missing"
          ],
          "title": "ConfigMissingReply",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "A file is present and does not read.",
          "properties": {
            "not_valid": {
              "$ref": "#/$defs/StateNotValid"
            }
          },
          "required": [
            "not_valid"
          ],
          "title": "NotValidReply",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The `state` reply is over the cap.",
          "properties": {
            "too_large": {
              "$ref": "#/$defs/StateTooLarge"
            }
          },
          "required": [
            "too_large"
          ],
          "title": "TooLargeReply",
          "type": "object"
        }
      ]
    },
    "JobId": {
      "description": "The id that attendance gives a thin job of the delegate door:\n`job-<ulid>` (contract 02 §2 and §12). Contract 02 calls it a session\nid. The `SessionId` of the job's row on the host session list is a\nseparate value.",
      "pattern": "^job-[0-9A-HJKMNP-TV-Z]{26}$",
      "type": "string"
    },
    "LitellmKey": {
      "description": "The family's LiteLLM key, `litellm_key` of `creds.json`. caregiver\nkeeps only a key that starts with `sk-` (`litellm_keys.py:155`). The\nrest is visible ASCII, with no space or control character, so the key\ncannot break the header that carries it. Decision CH31 keeps this rule\nand gives caregiver's check the same rule in a later change.",
      "pattern": "^sk-[!-~]*$",
      "type": "string"
    },
    "ModelAlias": {
      "description": "The LiteLLM alias of the family's model: `model.router` of the family\nfile, which caregiver writes as `model_alias` in `config/runtime.json`\n(`steps.py:242`).",
      "pattern": "^[a-z0-9][a-z0-9._/-]*$",
      "type": "string"
    },
    "PepToken": {
      "description": "The family token for the chaperone, `pep_token` of `creds.json`: 32\nbytes as 64 lowercase hex digits, the one form the chaperone takes\n(`credentials.py:21-25`).",
      "pattern": "^[0-9a-f]{64}$",
      "type": "string"
    },
    "SandboxGrant": {
      "additionalProperties": false,
      "description": "The built-in sandbox tools of Flue that the family grants: `sandbox_tools`\nof the family file, a subset of `SandboxTool` of `family/grammar.py`. The\nvalidator refuses a tool listed twice, so a grant is a set, and each tool\nis one key here. An empty grant gives no sandbox.",
      "properties": {
        "bash": {
          "description": "`bash`, the one way to run commands.",
          "type": "boolean"
        },
        "edit": {
          "description": "`edit`.",
          "type": "boolean"
        },
        "glob": {
          "description": "`glob`.",
          "type": "boolean"
        },
        "grep": {
          "description": "`grep`.",
          "type": "boolean"
        },
        "read": {
          "description": "`read`.",
          "type": "boolean"
        },
        "write": {
          "description": "`write`.",
          "type": "boolean"
        }
      },
      "required": [
        "bash",
        "edit",
        "glob",
        "grep",
        "read",
        "write"
      ],
      "type": "object"
    },
    "SessionCwd": {
      "description": "The `cwd` of a session: the absolute path that the client sent in\n`session/new`, or that attendance chose for a job. It is a display\nvalue and a `session/list` filter, never a guest path (decision CH5e,\nCH21-4).",
      "maxLength": 4096,
      "pattern": "^/[^\\u0000]*$",
      "type": "string"
    },
    "SessionId": {
      "description": "The id of one session, and of the VM that serves it (decision CH1).\n\nThe host mints the id at `session/new`. The text is a UUID in its\nlowercase hyphenated form, the form the facade mints today with\n`randomUUID`. A value of this type is always in that form. On the wire it\nis a JSON string, and a string of any other form is refused.",
      "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
      "type": "string"
    },
    "SessionOrigin": {
      "description": "Who made a session, kept on its row of the host session list (decision\nCH21-4). On the wire the variant name is the one key of an object.\n\nEach variant has a `title`. datamodel-codegen names a class of a `oneOf`\nmember by its title, and by its position without one.",
      "oneOf": [
        {
          "additionalProperties": false,
          "description": "A door client opened the session. No job owns it.",
          "properties": {
            "door": {
              "$ref": "#/$defs/Door"
            }
          },
          "required": [
            "door"
          ],
          "title": "DoorOrigin",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The delegate door opened the session for one thin job.",
          "properties": {
            "delegate": {
              "$ref": "#/$defs/Delegate"
            }
          },
          "required": [
            "delegate"
          ],
          "title": "DelegateOrigin",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "attendance opened the session for one autonomous job.",
          "properties": {
            "auto": {
              "$ref": "#/$defs/Auto"
            }
          },
          "required": [
            "auto"
          ],
          "title": "AutoOrigin",
          "type": "object"
        }
      ]
    },
    "SessionRow": {
      "additionalProperties": false,
      "description": "One row of the host session list (decision CH1, CH21, CH21-4). The vmm\nkeeps it, so `session/list` and the noticeboard need no VM boot.\n\nThe list holds the rows in the order the vmm stored them, newest first,\nas `session-index.ts:130-158` does today with its `rowid` (decision\nCH21-5). Two creates can share a millisecond, so the order is not the\norder of `created_at`. The list reply carries an opaque cursor on that\nstored order. The order is no field of the row.\n\nEach key is required. A field that may be `null` reads with\n`Option::deserialize`, so a missing key is refused, and the schema lists\nit in `required`.",
      "properties": {
        "created_at": {
          "$ref": "#/$defs/UnixMillis",
          "description": "When attendance created the session."
        },
        "cwd": {
          "$ref": "#/$defs/SessionCwd",
          "description": "The `cwd` that attendance sent in the create."
        },
        "family": {
          "$ref": "#/$defs/FamilyName",
          "description": "The family that serves the session."
        },
        "id": {
          "$ref": "#/$defs/SessionId",
          "description": "The id that attendance chose (decision CH25 rule 2)."
        },
        "origin": {
          "$ref": "#/$defs/SessionOrigin",
          "description": "Who made the session."
        },
        "title": {
          "anyOf": [
            {
              "$ref": "#/$defs/SessionTitle"
            },
            {
              "type": "null"
            }
          ],
          "description": "The first title from the facade, or `null` before a turn names the\nsession. The vmm sets it only while it is `null`, so the first title\nstays, as `nameOnce` keeps it today (`session-index.ts:122-125`)."
        },
        "updated_at": {
          "$ref": "#/$defs/UnixMillis",
          "description": "The time of the last Flue record of the last turn, from the facade.\nAt create the vmm sets it to `created_at`, as `SessionIndex.create`\ndoes today. Each turn end sets it again."
        }
      },
      "required": [
        "created_at",
        "cwd",
        "family",
        "id",
        "origin",
        "title",
        "updated_at"
      ],
      "type": "object"
    },
    "SessionTitle": {
      "description": "The title of a session on the host session list (decision CH21). The\nfacade makes it at the end of each turn, as `titleOf` makes it today,\nwith a trim of the end after the cut: the first line of the prompt\nwith words, white space folded to one space, cut to 256 bytes, then\ntrimmed.",
      "maxLength": 256,
      "minLength": 1,
      "pattern": "^[^\\u0009-\\u000D\\u0020\\u00A0\\u1680\\u2000-\\u200A\\u2028\\u2029\\u202F\\u205F\\u3000\\uFEFF]+( [^\\u0009-\\u000D\\u0020\\u00A0\\u1680\\u2000-\\u200A\\u2028\\u2029\\u202F\\u205F\\u3000\\uFEFF]+)*$",
      "type": "string"
    },
    "SkillName": {
      "description": "The name of a granted skill: `SKILL_NAME` of `family/grammar.py`,\nwhich has the rule of a family name.",
      "pattern": "^[a-z][a-z0-9-]{1,30}$",
      "type": "string"
    },
    "StartCount": {
      "description": "How many times guest init started the facade since the VM booted\n(decision CH19 rule 1). A restore keeps guest init's memory, so it keeps\nthe count. It is at least 1: a facade that never started has no count.\n\nOn the wire it is a JSON integer from 1 to 2^53 - 1, so JavaScript reads\nit exactly.",
      "format": "uint64",
      "maximum": 9007199254740991,
      "minimum": 1,
      "type": "integer"
    },
    "StateNotValid": {
      "additionalProperties": false,
      "description": "The reply of a pull when `creds.json` or `config/` is present and does\nnot read as `FamilyState`. It carries nothing, so it holds no value of\nthe files. The vmm names the file that failed in its host log only.",
      "type": "object"
    },
    "StateTooLarge": {
      "additionalProperties": false,
      "description": "The reply of a pull when the files read, and the `state` reply is over\nthe cap of 256 KiB (decision CH31). It carries nothing, not even the\nsize. The vmm writes the size in its host log only.",
      "type": "object"
    },
    "Timer": {
      "additionalProperties": false,
      "description": "The trigger of a cron firing. It carries nothing, because a timer has no\nname (contract 02 §13.2).",
      "type": "object"
    },
    "Trigger": {
      "description": "What fired an autonomous job, typed per kind (decision CH21-4a). A timer\nhas no name, and only a dispatch has a chain. There is no fired time: the\ncreated time of the row stands in for it.\n\nOn the wire the variant name is the one key of an object, as in\n`SessionOrigin`, and each variant has a `title` for its class name.",
      "oneOf": [
        {
          "additionalProperties": false,
          "description": "A cron firing.",
          "properties": {
            "timer": {
              "$ref": "#/$defs/Timer"
            }
          },
          "required": [
            "timer"
          ],
          "title": "TimerTrigger",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "A webhook firing.",
          "properties": {
            "webhook": {
              "$ref": "#/$defs/Webhook"
            }
          },
          "required": [
            "webhook"
          ],
          "title": "WebhookTrigger",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "A dispatch from another family.",
          "properties": {
            "dispatch": {
              "$ref": "#/$defs/Dispatch"
            }
          },
          "required": [
            "dispatch"
          ],
          "title": "DispatchTrigger",
          "type": "object"
        }
      ]
    },
    "TurnEnd": {
      "additionalProperties": false,
      "description": "A turn ended: it completed, the client cancelled it, or it failed. The\nfacade makes both fields as it makes them today, at the end of each\nturn (`facade/src/turns.ts:139-151`).",
      "properties": {
        "title": {
          "$ref": "#/$defs/SessionTitle",
          "description": "The title that `titleOf` makes from the prompt of this turn, with the\nend trimmed after the cut. The facade refuses a prompt with no text\nbefore the turn starts, so each turn has a title. The vmm stores it\nonly while the row's title is `null`, so the first title stays."
        },
        "updated_at": {
          "$ref": "#/$defs/UnixMillis",
          "description": "The time of the last Flue record of this turn, or the facade's clock\nwhen the turn wrote no record. The vmm stores it at each end."
        }
      },
      "required": [
        "title",
        "updated_at"
      ],
      "type": "object"
    },
    "TurnEvent": {
      "description": "What the facade tells guest init at each turn start and turn end, and\nwhat guest init relays to the vmm on control link 1200 (decision CH21).\nAt each end the vmm sets the row's `updated_at`, and it sets the row's\n`title` only while that is `null`, as `nameOnce` does today. It\nsnapshots a VM only between an end and the next start (decision CH19\nrule 6).\n\nThe event names no session: a VM serves one session (decision CH1), and\nthe link names the VM. On the wire the variant name is the one key of an\nobject, and each variant has a `title` for its class name.",
      "oneOf": [
        {
          "additionalProperties": false,
          "description": "A turn started.",
          "properties": {
            "start": {
              "$ref": "#/$defs/TurnStart"
            }
          },
          "required": [
            "start"
          ],
          "title": "TurnStartEvent",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "A turn ended.",
          "properties": {
            "end": {
              "$ref": "#/$defs/TurnEnd"
            }
          },
          "required": [
            "end"
          ],
          "title": "TurnEndEvent",
          "type": "object"
        }
      ]
    },
    "TurnStart": {
      "additionalProperties": false,
      "description": "A turn started. It carries nothing.\n\nIt is a type of its own so that the generated Python model is a class\nwith no field, as `Door` is.",
      "type": "object"
    },
    "UnixMillis": {
      "description": "A time on the wire: whole milliseconds since 1970-01-01T00:00:00Z, UTC\n(decision CH22-1). It is the one wire form of a time. No wire type holds\na `date-time` string.\n\nOn the wire it is a JSON integer from 0 to 2^53 - 1, so JavaScript reads\nit exactly. A larger integer, a negative integer, a float such as `1.0`\nor `1e3`, and a string are refused.",
      "format": "uint64",
      "maximum": 9007199254740991,
      "minimum": 0,
      "type": "integer"
    },
    "Webhook": {
      "additionalProperties": false,
      "description": "The trigger of a webhook firing.",
      "properties": {
        "name": {
          "$ref": "#/$defs/WebhookName",
          "description": "The name of the webhook that the family declares."
        }
      },
      "required": [
        "name"
      ],
      "type": "object"
    },
    "WebhookName": {
      "description": "The name of a declared webhook trigger: `WEBHOOK_NAME` of\n`family/grammar.py`, which has the rule of a family name.",
      "pattern": "^[a-z][a-z0-9-]{1,30}$",
      "type": "string"
    }
  },
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "x-routes": {
    "header": "creche-host-api-protocol",
    "protocol": 1,
    "routes": {
      "create": {
        "kind": "json",
        "reply": "SessionRow",
        "request": "CreateSession"
      }
    }
  }
} as const;
