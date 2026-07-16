import { mkdirSync, writeFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { StackSimple } from "@phosphor-icons/react/dist/ssr";
import React from "react";

mkdirSync(new URL("../public/", import.meta.url), { recursive: true });
const icon = renderToStaticMarkup(
  React.createElement(StackSimple, {
    alt: "mergetrain",
    color: "#0867ed",
    size: 64,
    weight: "bold",
  }),
);
writeFileSync(new URL("../public/favicon.svg", import.meta.url), icon, "utf8");
