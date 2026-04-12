import { defineConfig } from "vitest/config";

export default defineConfig({
    test: {
        environment: "jsdom",
        include: ["tests/frontend/**/*.test.js"],
        setupFiles: ["tests/frontend/setup.js"],
        globals: false,
    },
});
