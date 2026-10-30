// Adapted from jibbr (Stephen Blatt); MIT in whosaid.
import { describe, expect, test } from "bun:test";
import type { WordgraphNode } from "../src/wordgraph-shared";
import { balloonRadius, wrapBalloonLabel } from "../src/balloonWorld";

const node = (depth: number, label: string): WordgraphNode => ({
  id: `node-${depth}`,
  topicId: "topic",
  parentId: depth ? "root" : null,
  depth,
  label,
  description: "",
  source: depth ? "related" : "topic",
  evidence: [],
});

describe("balloon DOM geometry", () => {
  test("retains all Unicode and unbroken label text in visible wrapped lines", () => {
    const label =
      "超長いUnbrokenIdentifierThatMustRemainReadableWithoutEllipsis";
    const lines = wrapBalloonLabel(
      label,
      (value) => [...value].length * 10,
      54,
    );
    expect(lines.join("")).toBe(label);
    expect(
      lines.every((line) => [...line].length <= 5 || [...line].length === 1),
    ).toBe(true);
  });

  test("gives roots a moderate share-scaled disc while labels can grow beyond it", () => {
    const short = balloonRadius(
      node(0, "Short"),
      0.01,
      ["Short"],
      (value) => value.length * 7,
    );
    const large = balloonRadius(
      node(0, "Short"),
      1,
      ["Short"],
      (value) => value.length * 7,
    );
    const longLines = wrapBalloonLabel(
      "An intentionally long discussion phrase remains fully visible here",
      (value) => value.length * 8,
      70,
    );
    const long = balloonRadius(
      node(1, "long"),
      undefined,
      longLines,
      (value) => value.length * 8,
    );
    expect(short).toBeGreaterThanOrEqual(60);
    expect(large).toBeLessThanOrEqual(95);
    expect(large).toBeGreaterThan(short);
    expect(long).toBeGreaterThanOrEqual(55);
  });

  test("accounts for root's larger display font and multiline line height", () => {
    const lines = ["Large discussion", "label"];
    const root = balloonRadius(
      node(0, lines.join(" ")),
      0,
      lines,
      (value) => value.length * 8,
    );
    const child = balloonRadius(
      node(1, lines.join(" ")),
      undefined,
      lines,
      (value) => value.length * 8,
    );
    expect(root).toBeGreaterThan(child);
    expect(root).toBeGreaterThanOrEqual(60);
  });
});
