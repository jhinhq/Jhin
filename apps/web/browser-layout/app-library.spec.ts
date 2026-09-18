import { expect, test } from "@playwright/test";

for (const width of [320, 390, 768, 1024, 1280, 1440]) {
  for (const mode of ["curated", "catalog"]) {
    for (const readonly of [false, true]) {
      test(`${mode} ${width}px ${readonly ? "read-only" : "manager"} contents stay inside cards without overlap`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width, height: 1000 });
        await page.goto(`/?mode=${mode}&readonly=${readonly ? 1 : 0}`);
        await expect(page.getByTestId("app-supabase")).toBeVisible();
        await expect(page.locator('li[data-testid^="app-"], li[data-testid^="catalog-"]')).toHaveCount(mode === "catalog" ? 4 : 3);
        await page.evaluate(() => document.fonts.ready);
        const failures = await page.evaluate(() => {
          const errors: string[] = [];
          const cards = [...document.querySelectorAll<HTMLElement>('li[data-testid^="app-"], li[data-testid^="catalog-"]')];
          for (const card of cards) {
            const box = card.getBoundingClientRect();
            const parent = card.parentElement!.getBoundingClientRect();
            if (box.left < parent.left - 1 || box.right > parent.right + 1) errors.push(`${card.dataset.testid}: card bounds ${box.left.toFixed(1)}..${box.right.toFixed(1)} outside grid ${parent.left.toFixed(1)}..${parent.right.toFixed(1)}`);
            const elements = [...card.querySelectorAll<HTMLElement>('button, a, select, header span, h3')]
              .filter((el) => el.getBoundingClientRect().width > 0 && !el.querySelector("span"));
            for (const el of elements) {
              const rect = el.getBoundingClientRect();
              const label = el.getAttribute("aria-label") || el.textContent?.trim();
              if (rect.left < box.left - 1 || rect.right > box.right + 1 || rect.top < box.top - 1 || rect.bottom > box.bottom + 1)
                errors.push(`${card.dataset.testid}: ${label} bounds ${rect.left.toFixed(1)}..${rect.right.toFixed(1)} outside card ${box.left.toFixed(1)}..${box.right.toFixed(1)}`);
            }
            for (let i = 0; i < elements.length; i++) for (let j = i + 1; j < elements.length; j++) {
              const a = elements[i], b = elements[j];
              if (a.contains(b) || b.contains(a)) continue;
              const x = a.getBoundingClientRect(), y = b.getBoundingClientRect();
              const overlapX = Math.min(x.right, y.right) - Math.max(x.left, y.left);
              const overlapY = Math.min(x.bottom, y.bottom) - Math.max(x.top, y.top);
              if (overlapX > 1 && overlapY > 1) errors.push(`${card.dataset.testid}: ${a.textContent?.trim()} overlaps ${b.textContent?.trim()} by ${overlapX.toFixed(1)}x${overlapY.toFixed(1)}px`);
            }
          }
          if (document.documentElement.scrollWidth > innerWidth + 1) errors.push(`document width ${document.documentElement.scrollWidth} exceeds viewport ${innerWidth}`);
          return errors;
        });
        await page.screenshot({ path: testInfo.outputPath("cards.png"), fullPage: true });
        expect(failures).toEqual([]);
      });
    }
  }
}
