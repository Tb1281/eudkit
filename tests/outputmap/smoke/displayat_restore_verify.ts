import { FixedString, buildModule, every } from "../../../ts/index.ts";

export const module = buildModule(() => {
  const a = new FixedString(16);
  a.write("AAA");

  const b = new FixedString(16);
  b.write("BBB");

  every(6, () => {
    b.display();
  });

  a.displayAt(1);
});
