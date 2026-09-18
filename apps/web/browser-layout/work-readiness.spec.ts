import { expect, test, type Page } from "@playwright/test";

const stamp = "2026-09-12T00:00:00Z";
async function mockApi(page: Page) {
  const variables: Record<string, Record<string, unknown>[]> = {agent:[],team:[],company:[]};
  let schedules: Record<string, unknown>[] = [];
  const review = { review_id:"r1",connection_id:"ghost1",post_id:"post1",revision:"snapshot-7",status:"changes_requested",title:"A draft ready for review",author_agent_id:"a1",publisher_agent_id:"director",feedback:"Add a primary source before approval.",updated_at:stamp,url:"https://blog.example/post",html:"<h1>Saved draft revision</h1><p>The reviewed content.</p><script>parent.document.body.dataset.compromised='yes'</script>" };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url()), method = route.request().method(), path = url.pathname;
    const body = method === "GET" || method === "DELETE" ? {} : route.request().postDataJSON();
    const fulfill = (json: unknown, status = 200) => route.fulfill({ status, json });
    if (path.endsWith("/org-graph")) return fulfill({teams:[{id:"t1",name:"Marketing"}],agents:[{id:"a1",name:"Blogger"}]});
    if (path.includes("/editorial-reviews")) return fulfill(path.endsWith("/r1") ? review : {items:[review],next_before_id:null});
    if (path.includes("/memories/summary")) return fulfill({scope:url.searchParams.get("scope"),scope_id:url.searchParams.get("scope_id"),version:"facts-v3",summary:"Publish only after the director approves the exact draft.",coverage_count:2,source_count:1,generated_at:stamp,stale:false,items:[{id:"m1",version:3,content:"The director approves drafts.",source_conversation_id:"c1",source_message_id:"m1",source_task_id:null}]});
    if (path.endsWith("/occurrences")) return fulfill({items:[{id:"o1",scheduled_for:stamp,status:"completed",task_id:"task1"}],total:1});
    if (path.includes("/schedules")) {
      if (method === "POST") schedules.push({...body,id:"s1",version:1,next_run_at:"2026-09-14T16:00:00Z",last_status:null,created_at:stamp,updated_at:stamp});
      if (method === "PATCH") { schedules=schedules.map((item)=>({...item,...body,version:Number(item.version)+1})); return fulfill(schedules[0]); }
      if (method === "DELETE") { schedules=[]; return route.fulfill({status:204}); }
      return fulfill(method === "GET" ? {items:schedules,total:schedules.length} : schedules[0]);
    }
    if (path.includes("/variables")) {
      if (method === "GET") { const items=variables[url.searchParams.get("scope") ?? "agent"]; return fulfill({items,total:items.length}); }
      if(path.endsWith("/copy")) { const source=Object.values(variables).flat().find((item)=>path.includes(`/${item.id}/`));const row={...source,id:"copy1",scope:body.scope,scope_id:body.scope_id,name:body.name,source_variable_id:source?.id,source_version:source?.version};variables[body.scope].push(row);return fulfill(row,201); }
      if (method === "POST") {
        const row = {id:`v${Object.values(variables).flat().length+1}`,workspace_id:"ws",name:body.name,scope:body.scope,scope_id:body.scope_id,sensitive:body.sensitive,configured:true,version:1,description:body.description,created_by_type:"user",created_by_id:"u1",created_at:stamp,updated_at:stamp,...(!body.sensitive?{value:body.value}:{})};
        variables[body.scope].push(row); return fulfill(row,201);
      }
      const variableId = path.split("/variables/")[1]?.split("/")[0];
      for(const scope of Object.keys(variables)) { const item=variables[scope].find((item)=>item.id===variableId); if(!item) continue;
        if(Number(url.searchParams.get("expected_version") ?? body.expected_version)!==item.version) return fulfill({detail:"Stale version"},409);
        if(method === "DELETE") {variables[scope]=variables[scope].filter((value)=>value!==item); return route.fulfill({status:204});}
        if(!item.sensitive) item.value=body.value; item.version=Number(item.version)+1; return fulfill(item);
      }
    }
    return fulfill({detail:"Unexpected fixture request"},404);
  });
}

for (const width of [390,1440]) test(`scoped variables, schedules, memory and director reviews at ${width}px`, async ({page},info) => {
  await page.setViewportSize({width,height:950}); await mockApi(page); await page.goto("/work-readiness.html");
  await page.getByRole("heading",{name:"Agent work settings"}).waitFor();
  await page.getByRole("button",{name:"Add variable",exact:true}).click();
  await page.getByLabel("Name",{exact:true}).fill("ADMIN_KEY"); await page.getByLabel("Sensitive value",{exact:true}).check();
  await page.getByLabel("New secret value",{exact:true}).fill("browser-test-private-value"); await page.getByRole("button",{name:"Save secret",exact:true}).click();
  await expect(page.getByText("ADMIN_KEY",{exact:true})).toBeVisible(); await expect(page.getByText("Secret configured",{exact:true})).toBeVisible();
  await expect(page.locator("body")).not.toContainText("browser-test-private-value");
  expect(await page.evaluate(()=>JSON.stringify({...localStorage}))).not.toContain("browser-test-private-value");
  await page.getByRole("button",{name:"Replace secret"}).click(); await page.getByLabel("New secret value").fill("replacement-test-only"); await page.keyboard.press("Escape");
  await page.getByRole("button",{name:"Replace secret"}).click(); await expect(page.getByLabel("New secret value")).toHaveValue(""); await page.keyboard.press("Escape");
  await page.getByRole("button",{name:"Copy to scope"}).click(); await page.getByRole("combobox",{name:"Destination"}).selectOption("company:ws"); await expect(page.getByLabel("New secret value")).toHaveCount(0); await page.getByRole("button",{name:"Copy variable",exact:true}).click();
  await page.getByRole("button",{name:"Variables & memory"}).click(); const resources=page.getByRole("dialog");
  await expect(resources.getByText("ADMIN_KEY",{exact:true})).toBeVisible();
  for (const [scope,name] of [["","COMPANY_URL"],["t1","TEAM_URL"]]) {
    await resources.getByRole("combobox",{name:"Scope",exact:true}).selectOption(scope);
    await resources.getByRole("button",{name:"Add variable",exact:true}).click(); await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog",{name:"Company & team context"})).toBeVisible();
    await resources.getByRole("button",{name:"Add variable",exact:true}).click(); const form=page.getByRole("dialog",{name:"Add variable"});
    await form.getByLabel("Name",{exact:true}).fill(name); await form.getByLabel("Value",{exact:true}).fill("https://blog.example"); await form.getByRole("button",{name:"Save variable"}).click();
    await expect(resources.getByText(name,{exact:true})).toBeVisible();
    const saved = resources.locator("li").filter({has:page.getByRole("heading",{name,exact:true})});
    await saved.getByRole("button",{name:"Edit",exact:true}).click();
    await expect(page.getByLabel("Value",{exact:true})).toHaveValue("https://blog.example");
    await page.getByLabel("Description",{exact:true}).fill("Saved context");
    await page.getByLabel("Value",{exact:true}).fill("https://blog.example/revised");
    await page.getByRole("button",{name:"Save variable",exact:true}).click();
    await expect(saved).toContainText("https://blog.example/revised");
  }
  await resources.getByText("Current memory summary",{exact:true}).click(); await expect(resources.getByText(/Publish only after the director/)).toBeVisible();
  await resources.getByText("Sources and versions",{exact:true}).click(); await expect(resources.getByRole("link",{name:"Source chat"})).toHaveAttribute("href","/chats/c1");
  await page.screenshot({path:info.outputPath(`company-context-${width}.png`),fullPage:true});
  await resources.getByRole("button",{name:"Done",exact:true}).click();
  await page.getByRole("button",{name:"New schedule",exact:true}).click();
  await page.getByLabel("Name",{exact:true}).fill("Daily article draft"); await page.getByLabel("Standing brief",{exact:true}).fill("Prepare an article. Ask the director to review; do not publish it yourself.");
  await page.getByLabel("Timezone",{exact:true}).fill("America/Los_Angeles"); await page.getByRole("button",{name:"Save schedule"}).click();
  await expect(page.getByText("Daily article draft",{exact:true})).toBeVisible(); await page.getByRole("button",{name:"Pause schedule"}).click(); await expect(page.getByRole("button",{name:"Resume schedule"})).toBeVisible();
  await page.getByRole("button",{name:"Run history",exact:true}).click(); await expect(page.getByRole("link",{name:"View task"})).toHaveAttribute("href","/tasks/task1");
  await page.screenshot({path:info.outputPath(`settings-${width}.png`),fullPage:true});
  await page.getByRole("button",{name:"Inspect review"}).click();
  await expect(page.frameLocator('iframe[title="Reviewed draft preview"]').getByText("Saved draft revision")).toBeVisible();
  expect(await page.locator("body").getAttribute("data-compromised")).toBeNull();
  await expect(page.getByRole("button",{name:"Publish",exact:true})).toHaveCount(0);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath(`work-readiness-${width}.png`),fullPage:true});
  await page.keyboard.press("Escape"); await expect(page.getByRole("button",{name:"Inspect review"})).toBeFocused();
});
