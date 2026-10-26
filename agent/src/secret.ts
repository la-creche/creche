// A credential as a value that cannot become text by accident. The object
// holds no field: the text lives in a module map. `String()` gives
// "[object Object]", `JSON.stringify` gives "{}" and `inspect` gives
// "Secret {}". The class has no `toString` and no `toJSON` of its own.
//
// Only two places unwrap a secret: the `auth` of the `ch` connection and the
// `auth` of the LiteLLM provider. `test/secret.test.ts` holds that list.

const TEXT = new WeakMap<Secret, string>();

export class Secret {
  private constructor() {}

  public static of(text: string): Secret {
    const secret = new Secret();
    TEXT.set(secret, text);

    return Object.freeze(secret);
  }
}

export function revealSecret(secret: Secret): string {
  return TEXT.get(secret) ?? "";
}
