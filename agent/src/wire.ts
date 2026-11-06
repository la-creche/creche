// The JSON Schema of the wire types, a copy of rust/crates/wire/schema/wire.json
// (decision CH22). The schema test of rust/crates/wire writes this file. Never
// edit it: run `CRECHE_WIRE_SCHEMA=write cargo test -p creche-wire` in rust/.

export const wire = {
  "$defs": {
    "AcpError": {
      "description": "The error of the route `acp`. The vmm sends each code before 101.",
      "oneOf": [
        {
          "additionalProperties": false,
          "description": "The request has no `Upgrade` header, or the header does not\nname `AcpRoute::TOKEN` (decision CH26 rule 3). The vmm also\nsends `Upgrade: creche-acp` with this error (RFC 9110).",
          "properties": {
            "code": {
              "const": "upgrade_required",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "No row has the id, the row belongs to another family, or a drop\nof the session runs (decisions CH21-9, CH33 rule 5).",
          "properties": {
            "code": {
              "const": "not_found",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The session has an open ACP stream (decision CH33 rule 7).\nattendance opens one stream per session.",
          "properties": {
            "code": {
              "const": "busy",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The VM does not fit on the host, and no idle VM can make room\n(decisions CH8-6, CH21-3, CH21-9). The vmm never queues: the\nclient retries.",
          "properties": {
            "code": {
              "const": "host_full",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The VM did not boot or restore, or guest init sent no\n`ControlReady` (decisions CH6-3, CH33 rule 6). The vmm also\nwrites the fault to its fault file (decision CH21-1).",
          "properties": {
            "code": {
              "const": "sandbox_start_failed",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The VM failed the deny-by-default check of its start (decisions\nCH6-3, CH33 rule 6). The vmm also writes the fault to its fault\nfile (decision CH21-1).",
          "properties": {
            "code": {
              "const": "egress_assert_failed",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "No route has this path.",
          "properties": {
            "code": {
              "const": "no_route",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The route has this path, but the method is not `POST`.",
          "properties": {
            "code": {
              "const": "bad_method",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The `Content-Type` is not `application/json`.",
          "properties": {
            "code": {
              "const": "bad_content_type",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The body is over the cap.",
          "properties": {
            "code": {
              "const": "too_large",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The body is not strict JSON.",
          "properties": {
            "code": {
              "const": "not_json",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The body is JSON, but not the request type of the route.",
          "properties": {
            "code": {
              "const": "not_valid",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The vmm failed after it took the request. The vmm logs\nthe details.",
          "properties": {
            "code": {
              "const": "internal",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        }
      ]
    },
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
    "ControlCommand": {
      "description": "What the vmm sends on control link 1200 after `ControlHello` (decision\nCH36 G11). Guest init sends `stop` on its local link to the facade too\n(decision CH62 rule 10).\n\n`stop` starts the graceful stop of decision CH19 rule 3. Guest init\nstops the facade, for up to 25 s when a turn runs (decision CH62 rule\n9). It then syncs the disks and powers the VM off. The vmm sends\nSIGTERM, then SIGKILL, only when the VM does not exit in time, because\na kill is a power cut for the guest.\n\n`sync` asks guest init to sync the session disk before a snapshot\n(decision CH62 rule 23, item 5.7). Guest init runs `syncfs` of the\nsession disk, then `sync`, and answers with `GuestEvent::Synced`. A VM\nwith no `synced` in time gets no snapshot, and the graceful stop runs.\n\nOn the wire the variant name is the one key of an object, and each\nvariant has a `title` for its class name. An externally tagged enum has\nno unit variant, so each variant holds an empty object (decision CH22\nrule 3).",
      "oneOf": [
        {
          "additionalProperties": false,
          "description": "Stop the facade, sync and power off.",
          "properties": {
            "stop": {
              "$ref": "#/$defs/ControlStop"
            }
          },
          "required": [
            "stop"
          ],
          "title": "ControlStopCommand",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "Sync the session disk, then answer `synced`.",
          "properties": {
            "sync": {
              "$ref": "#/$defs/ControlSync"
            }
          },
          "required": [
            "sync"
          ],
          "title": "ControlSyncCommand",
          "type": "object"
        }
      ]
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
    "ControlStop": {
      "additionalProperties": false,
      "description": "The graceful stop. It carries nothing.\n\nIt is a type of its own so that the generated Python model is a class\nwith no field, as `TurnStart` is.",
      "type": "object"
    },
    "ControlSync": {
      "additionalProperties": false,
      "description": "The guest sync before a snapshot. It carries nothing (decision CH62\nrule 23).\n\nIt is a type of its own so that the generated Python model is a class\nwith no field, as `ControlStop` is.",
      "type": "object"
    },
    "ControlSynced": {
      "additionalProperties": false,
      "description": "The answer of guest init to `ControlCommand::Sync`: the session disk\nand every other disk are synced (decision CH62 rule 23). It carries\nnothing.\n\nIt is a type of its own so that the generated Python model is a class\nwith no field, as `ControlStop` is.",
      "type": "object"
    },
    "CreateError": {
      "description": "The error of the route `create`.",
      "oneOf": [
        {
          "additionalProperties": false,
          "description": "A row with this id exists, and it was made from a different\nrequest (decisions CH25 rule 2, CH33 rule 12). Two requests\ndiffer when their typed values differ, not their bytes.",
          "properties": {
            "code": {
              "const": "conflict",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The family has no `config/` dir, so the vmm refuses the create\n(decision CH33 rule 3). caregiver removes the family files\nbefore it drops the family.",
          "properties": {
            "code": {
              "const": "unknown_family",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "No route has this path.",
          "properties": {
            "code": {
              "const": "no_route",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The route has this path, but the method is not `POST`.",
          "properties": {
            "code": {
              "const": "bad_method",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The `Content-Type` is not `application/json`.",
          "properties": {
            "code": {
              "const": "bad_content_type",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The body is over the cap.",
          "properties": {
            "code": {
              "const": "too_large",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The body is not strict JSON.",
          "properties": {
            "code": {
              "const": "not_json",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The body is JSON, but not the request type of the route.",
          "properties": {
            "code": {
              "const": "not_valid",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The vmm failed after it took the request. The vmm logs\nthe details.",
          "properties": {
            "code": {
              "const": "internal",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        }
      ]
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
    "DropError": {
      "description": "The error of the route `drop`.",
      "oneOf": [
        {
          "additionalProperties": false,
          "description": "No row has the id of a `SessionDrop`, or the row belongs to\nanother family (decision CH33 rules 4 and 5). A drop of a family\nnever gives it.",
          "properties": {
            "code": {
              "const": "not_found",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "No route has this path.",
          "properties": {
            "code": {
              "const": "no_route",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The route has this path, but the method is not `POST`.",
          "properties": {
            "code": {
              "const": "bad_method",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The `Content-Type` is not `application/json`.",
          "properties": {
            "code": {
              "const": "bad_content_type",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The body is over the cap.",
          "properties": {
            "code": {
              "const": "too_large",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The body is not strict JSON.",
          "properties": {
            "code": {
              "const": "not_json",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The body is JSON, but not the request type of the route.",
          "properties": {
            "code": {
              "const": "not_valid",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The vmm failed after it took the request. The vmm logs\nthe details.",
          "properties": {
            "code": {
              "const": "internal",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        }
      ]
    },
    "DropFamily": {
      "additionalProperties": false,
      "description": "Every session of one family (decision CH21-10). A family with no rows\ngives `Dropped` too, so this drop has no `not_found`.",
      "properties": {
        "family": {
          "$ref": "#/$defs/FamilyName",
          "description": "The family whose sessions the vmm drops."
        }
      },
      "required": [
        "family"
      ],
      "type": "object"
    },
    "DropSession": {
      "additionalProperties": false,
      "description": "One session to drop. It names the family that owns the session (decision\nCH33 rule 5). The vmm answers `not_found` when no row has the id, and\nwhen the row of the id belongs to another family. So a client learns\nnothing of the sessions of another family.",
      "properties": {
        "family": {
          "$ref": "#/$defs/FamilyName",
          "description": "The family that owns the session."
        },
        "id": {
          "$ref": "#/$defs/SessionId",
          "description": "The id of the session."
        }
      },
      "required": [
        "family",
        "id"
      ],
      "type": "object"
    },
    "DropTarget": {
      "description": "What a client sends to the vmm to drop sessions: the request of the drop\nroute. On the wire the variant name is the one key of an object, as in\n`SessionOrigin`, and each variant has a `title` for its class name.\n\nattendance drops one session on an ACP `session/delete` (decision CH27)\nand at the end of a job (decision CH21-2). caregiver drops a family when\nit deletes the family (decision CH21-10).",
      "oneOf": [
        {
          "additionalProperties": false,
          "description": "One session of one family.",
          "properties": {
            "session": {
              "$ref": "#/$defs/DropSession"
            }
          },
          "required": [
            "session"
          ],
          "title": "SessionDrop",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "Every session of one family.",
          "properties": {
            "family": {
              "$ref": "#/$defs/DropFamily"
            }
          },
          "required": [
            "family"
          ],
          "title": "FamilyDrop",
          "type": "object"
        }
      ]
    },
    "Dropped": {
      "additionalProperties": false,
      "description": "The reply of the drop route. It carries nothing: the vmm sends it after\nthe target has no VM, no snapshot, no session disk and no row.\n\nIt is a type of its own so that the generated Python model is a class\nwith no field. An inline empty object generates a `dict` with lax keys.",
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
    "FamilyCpus": {
      "description": "The vCPUs of the family VM: `sandbox.cpus` of the family file,\n`CPUS_MIN` to `CPUS_MAX` of `family/grammar.py`. The vmm gives the\nguest this many vCPUs and the scope this many CPUs of quota\n(decision CH8-5).",
      "format": "uint64",
      "maximum": 8,
      "minimum": 1,
      "type": "integer"
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
    "FamilyMemoryMib": {
      "description": "The guest RAM of the family VM in MiB: `sandbox.memory` of the\nfamily file as `memory_mb` of `family/grammar.py` reads it, which\ncounts `1g` as 1024. The range is `MEMORY_MIN_MB` to\n`MEMORY_MAX_MB` there. The scope gets a memory cap above it\n(decision CH8-5).",
      "format": "uint64",
      "maximum": 16384,
      "minimum": 256,
      "type": "integer"
    },
    "FamilyName": {
      "description": "The name of a family: `FAMILY_NAME` of `family/grammar.py`.",
      "pattern": "^[a-z][a-z0-9-]{1,30}$",
      "type": "string"
    },
    "FamilyShape": {
      "additionalProperties": false,
      "description": "The family VM shape: the file `FAMILY_SHAPE_FILE` in the family's\n`config/` (decision CH36 G6). caregiver writes it from the family file.\nThe vmm reads it before it starts a VM of the family (decision CH8-5).\n\nIt is a file of its own, not a part of `runtime.json`, so the guest\nreads none of it. `family/grammar.py` stays the authority for the\nranges and the flavors (decision CH23 rule 5).\n`wire/tests/test_wire_grammar.py` holds them equal. No route reaches\nthe type, so it needs no new `HOST_API_PROTOCOL` (decision CH33 rule 8).",
      "properties": {
        "cpus": {
          "$ref": "#/$defs/FamilyCpus",
          "description": "The vCPUs."
        },
        "flavor": {
          "description": "The image flavor: `sandbox.image` of the family file.",
          "enum": [
            "base",
            "python"
          ],
          "title": "SandboxFlavor",
          "type": "string"
        },
        "memory_mib": {
          "$ref": "#/$defs/FamilyMemoryMib",
          "description": "The guest RAM in MiB."
        }
      },
      "required": [
        "cpus",
        "flavor",
        "memory_mib"
      ],
      "type": "object"
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
    "FamilyStatePull": {
      "additionalProperties": false,
      "description": "What the facade sends on host port 1100 to pull the family state,\nbefore each prompt (decisions CH5a, CH43 rule 4). It carries nothing:\nthe facade sends no revision, and the vmm sends the full typed state on\nevery pull (decision CH23).\n\nThe facade connects to `guest::FAMILY_STATE_SOCKET`, and guest init\nsplices each connection to a new vsock dial of host port 1100 (decision\nCH62 rule 13). The facade may send many pulls on one link. The vmm\nanswers each pull with one `FamilyStateReply`.\n\nIt is a type of its own so that the generated Python model is a class\nwith no field, as `ControlStop` is.",
      "type": "object"
    },
    "FamilyStateReply": {
      "description": "What the vmm answers on host port 1100 to each `FamilyStatePull`, before\neach prompt (decisions CH5a, CH5b, CH23). The vmm reads caregiver's\n`creds.json` and `config/` at each pull.\n\nA read failure is a typed reply, and the link stays open (decision CH23\ndefault 2). The facade fails that prompt and keeps running. At start it\npulls again with a backoff, and never exits on a typed error. When\n`creds.json` and `config/` are both absent, `creds_missing` wins\n(decision CH31).\n\nA reply text has a cap of 256 KiB, `FAMILY_STATE_CAP` (decision CH23\ndefault 4). The cap is a rule of the read surface, not of this type:\nthe vmm's writer and the `cap` of `creche_wire.read` hold it, because\nJSON Schema counts code points, not bytes. A `state` reply over the cap is `too_large`, not\n`not_valid` (decision CH31).\n\nThe vmm reads `model_live_at` from `config/runtime.json` (decision\nCH23-1). It holds the `state` reply until the host time passes that\ntime, for at most 10 s (decision CH31). No wire type carries\n`model_live_at`, and the guest needs no clock for it. A held pull can\ntake up to 10 s longer.\n\nOn the wire the variant name is the one key of an object, and each\nvariant has a `title` for its class name.",
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
    "GuestEvent": {
      "description": "What guest init sends on control link 1200 after `ControlReady`, each\nat any time (decisions CH21, CH36 G11).\n\n1. `facade`: the facade state changed (decision CH19 rule 2). Guest init\n   sends each change, so the vmm sees a facade that opened its local\n   link and is up (decision CH32 rule 6). The vmm applies it as it\n   applies the state of `ControlReady`.\n2. `synced`: the answer to `ControlCommand::Sync` (decision CH62 rule\n   23).\n3. `turn`: a turn event of the facade, relayed (decision CH21). The\n   facade sends the bare `TurnEvent` on its local link, and guest init\n   nests it here (decision CH62 rules 10 and 11). A turn event with no\n   control link open is dropped, and the next `ControlReady` carries\n   the last `TurnEnd`.\n\nOn the wire the variant name is the one key of an object, and each\nvariant has a `title` for its class name.",
      "oneOf": [
        {
          "additionalProperties": false,
          "description": "The facade state after a change.",
          "properties": {
            "facade": {
              "$ref": "#/$defs/FacadeState"
            }
          },
          "required": [
            "facade"
          ],
          "title": "GuestFacadeEvent",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The disks are synced.",
          "properties": {
            "synced": {
              "$ref": "#/$defs/ControlSynced"
            }
          },
          "required": [
            "synced"
          ],
          "title": "GuestSyncedEvent",
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "A turn started or ended.",
          "properties": {
            "turn": {
              "$ref": "#/$defs/TurnEvent"
            }
          },
          "required": [
            "turn"
          ],
          "title": "GuestTurnEvent",
          "type": "object"
        }
      ]
    },
    "JobId": {
      "description": "The id that attendance gives a thin job of the delegate door:\n`job-<ulid>` (contract 02 §2 and §12). Contract 02 calls it a session\nid. The `SessionId` of the job's row on the host session list is a\nseparate value.",
      "pattern": "^job-[0-9A-HJKMNP-TV-Z]{26}$",
      "type": "string"
    },
    "ListCursor": {
      "description": "A place on the stored order of the host session list (decision CH21-5).\nThe vmm gives it as `next` of a `SessionPage`, and the client sends it\nback as `cursor` of the next `ListSessions`. It is opaque: a client never\nmakes one or reads a meaning from it.\n\nOn the wire it is a JSON integer from 1 to 2^53 - 1, so JavaScript reads\nit exactly. Any such integer reads, so the list route has no cursor\nerror. attendance turns it into the string cursor of ACP `session/list`.",
      "format": "uint64",
      "maximum": 9007199254740991,
      "minimum": 1,
      "type": "integer"
    },
    "ListError": {
      "description": "The error of the route `list`. It holds the shared codes only: a\nfamily with no rows gives an empty page, and any cursor reads.",
      "oneOf": [
        {
          "additionalProperties": false,
          "description": "No route has this path.",
          "properties": {
            "code": {
              "const": "no_route",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The route has this path, but the method is not `POST`.",
          "properties": {
            "code": {
              "const": "bad_method",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The `Content-Type` is not `application/json`.",
          "properties": {
            "code": {
              "const": "bad_content_type",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The body is over the cap.",
          "properties": {
            "code": {
              "const": "too_large",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The body is not strict JSON.",
          "properties": {
            "code": {
              "const": "not_json",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The body is JSON, but not the request type of the route.",
          "properties": {
            "code": {
              "const": "not_valid",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        },
        {
          "additionalProperties": false,
          "description": "The vmm failed after it took the request. The vmm logs\nthe details.",
          "properties": {
            "code": {
              "const": "internal",
              "type": "string"
            }
          },
          "required": [
            "code"
          ],
          "type": "object"
        }
      ]
    },
    "ListSessions": {
      "additionalProperties": false,
      "description": "What a client sends to the vmm for one page of the host session list\n(decisions CH21-5, CH21-7). The list is scoped to one family, as the\nlist entry of the route table plan gives it. A family with no rows, or\none the vmm does not know, gives an empty page, so the list route has no\nerror of its own.\n\nEach key is required, and `null` is the one form of no value.",
      "properties": {
        "cursor": {
          "anyOf": [
            {
              "$ref": "#/$defs/ListCursor"
            },
            {
              "type": "null"
            }
          ],
          "description": "The `next` of the page before, or `null` for the first page."
        },
        "cwd": {
          "anyOf": [
            {
              "$ref": "#/$defs/SessionCwd"
            },
            {
              "type": "null"
            }
          ],
          "description": "Only the rows with this `cwd`, or `null` for every row. The vmm\napplies it before it cuts the page, as `session-index.ts` does today."
        },
        "family": {
          "$ref": "#/$defs/FamilyName",
          "description": "The family whose rows the page holds."
        }
      },
      "required": [
        "cursor",
        "cwd",
        "family"
      ],
      "type": "object"
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
    "OpenAcp": {
      "additionalProperties": false,
      "description": "The session of an ACP stream: the request of the `acp` Upgrade route. It\nnames the family that owns the session (decision CH33 rule 5). The vmm\nanswers `not_found` when no row has the id, and when the row of the id\nbelongs to another family. So a client learns nothing of the sessions\nof another family.",
      "properties": {
        "family": {
          "$ref": "#/$defs/FamilyName",
          "description": "The family that owns the session."
        },
        "id": {
          "$ref": "#/$defs/SessionId",
          "description": "The id of the session."
        }
      },
      "required": [
        "family",
        "id"
      ],
      "type": "object"
    },
    "PepToken": {
      "description": "The family token for the chaperone, `pep_token` of `creds.json`: 32\nbytes as 64 lowercase hex digits, the one form the chaperone takes\n(`credentials.py:21-25`).",
      "pattern": "^[0-9a-f]{64}$",
      "type": "string"
    },
    "ProtocolMismatch": {
      "additionalProperties": false,
      "description": "The one reply of the vmm when the protocol header of a request is\nmissing or names another protocol number (decisions CH25 rule 1, CH33\nrule 1). The vmm checks the header before the route, so each route and\neach path gets this reply. The wire form is\n`{\"code\":\"protocol_mismatch\",\"protocol\":<n>}`, with the status\n`MISMATCH_STATUS`.\n\nThe reply is frozen for all protocol numbers: a new protocol number\nnever changes its body or its status. A client of an older protocol\nmust still read it. A test pins the bytes and the status.\n\nIt is in no route error. A client checks the status `MISMATCH_STATUS`\nbefore it reads a route error.",
      "properties": {
        "code": {
          "const": "protocol_mismatch",
          "description": "Always `protocol_mismatch`.",
          "type": "string"
        },
        "protocol": {
          "description": "The host API protocol of the vmm that answers: its\n`HOST_API_PROTOCOL`.",
          "format": "uint32",
          "maximum": 4294967295,
          "minimum": 0,
          "type": "integer"
        }
      },
      "required": [
        "code",
        "protocol"
      ],
      "type": "object"
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
    "SessionPage": {
      "additionalProperties": false,
      "description": "One page of the host session list: the reply of the list route. The rows\nare newest stored first (decision CH21-5).\n\nThe vmm sets the most rows of a page. The page also ends early when one\nmore row would take the reply past `HOST_API_BODY_CAP` (decision CH33\nrule 2). A client then gets fewer rows and a `next` that is not `null`.\nThe largest row is far under the cap, so a page with a `next` holds at\nleast one row.",
      "properties": {
        "next": {
          "anyOf": [
            {
              "$ref": "#/$defs/ListCursor"
            },
            {
              "type": "null"
            }
          ],
          "description": "The cursor of the next page, or `null` when no row follows."
        },
        "rows": {
          "description": "The rows of this page.",
          "items": {
            "$ref": "#/$defs/SessionRow"
          },
          "type": "array"
        }
      },
      "required": [
        "next",
        "rows"
      ],
      "type": "object"
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
      "description": "What the facade tells guest init at each turn start and turn end, on\nits local link (decision CH62 rule 10). Guest init relays it to the vmm\non control link 1200 inside `GuestEvent::Turn` (decision CH21).\nAt each end the vmm sets the row's `updated_at`, and it sets the row's\n`title` only while that is `null`, as `nameOnce` does today. It\nsnapshots a VM only between an end and the next start (decision CH19\nrule 6).\n\nThe event names no session: a VM serves one session (decision CH1), and\nthe link names the VM. On the wire the variant name is the one key of an\nobject, and each variant has a `title` for its class name.",
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
    "body_cap": 1048576,
    "header": "creche-host-api-protocol",
    "mismatch": {
      "reply": "ProtocolMismatch",
      "status": 412
    },
    "protocol": 1,
    "routes": {
      "acp": {
        "error": "AcpError",
        "kind": "upgrade",
        "request": "OpenAcp",
        "statuses": {
          "bad_content_type": 415,
          "bad_method": 405,
          "busy": 409,
          "egress_assert_failed": 502,
          "host_full": 503,
          "internal": 500,
          "no_route": 404,
          "not_found": 404,
          "not_json": 400,
          "not_valid": 400,
          "sandbox_start_failed": 502,
          "too_large": 413,
          "upgrade_required": 426
        },
        "token": "creche-acp"
      },
      "create": {
        "error": "CreateError",
        "kind": "json",
        "reply": "SessionRow",
        "request": "CreateSession",
        "statuses": {
          "bad_content_type": 415,
          "bad_method": 405,
          "conflict": 409,
          "internal": 500,
          "no_route": 404,
          "not_json": 400,
          "not_valid": 400,
          "too_large": 413,
          "unknown_family": 404
        }
      },
      "drop": {
        "client_timeout_s": 60,
        "error": "DropError",
        "kind": "json",
        "reply": "Dropped",
        "request": "DropTarget",
        "statuses": {
          "bad_content_type": 415,
          "bad_method": 405,
          "internal": 500,
          "no_route": 404,
          "not_found": 404,
          "not_json": 400,
          "not_valid": 400,
          "too_large": 413
        }
      },
      "list": {
        "error": "ListError",
        "kind": "json",
        "reply": "SessionPage",
        "request": "ListSessions",
        "statuses": {
          "bad_content_type": 415,
          "bad_method": 405,
          "internal": 500,
          "no_route": 404,
          "not_json": 400,
          "not_valid": 400,
          "too_large": 413
        }
      }
    }
  }
} as const;
