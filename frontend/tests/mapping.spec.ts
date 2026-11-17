import { expect, test } from '@playwright/test'

/**
 * 维度成员映射版本化刷新的页面级验收：
 * 幂等刷新、成员级稳定更新、连续刷新只落一个有效版本、冲突核对、分析消费持久化、
 * 回退后重新进入页面成员归属保持不变、对照导出。
 */
const ORIGINAL_MEMBERS = [
  'M001,华东大区,',
  'M002,上海,M001',
  'M003,江苏,M001',
  'M004,浙江,M001',
  'M005,南京,M003',
  'M006,杭州,M004',
  'M007,华南大区,',
  'M008,广东,M007',
  'M009,深圳,M008',
  'M010,苏州,M003',
].join('\n')

const V4_MEMBERS = [
  'M001,华东大区,',
  'M002,上海,M001',
  'M003,江苏,M001',
  'M004,浙江,M001',
  'M005,南京,M003',
  'M006,杭州,M004',
  'M007,华南大区,',
  'M008,广东,M007',
  'M009,深圳,M008',
  'M010,苏州,M001',
  'M011,宁波,M004',
].join('\n')

test('维度成员映射：版本化刷新、并发边界、核对回退与导出', async ({ page }) => {
  test.setTimeout(120_000)
  const browserErrors: string[] = []
  page.on('pageerror', (e) => browserErrors.push(e.message))
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 400) browserErrors.push(`${r.status()} ${r.url()}`)
  })

  await page.goto('/dimension_mapping')
  await expect(page.getByTestId('head-version')).toHaveText('MAP-REGION-0003')
  await expect(page.locator('[data-testid^="mapping-row-"]')).toHaveCount(10)
  await expect(page.getByTestId('pending-count')).toHaveText('0')

  // ---- 1. 相同输入刷新：幂等，不产生新版本 ----
  await page.getByTestId('tab-refresh').click()
  await page.getByTestId('refresh-members').fill(ORIGINAL_MEMBERS)
  await page.getByTestId('submit-refresh').click()
  await expect(page.getByTestId('notice')).toContainText('幂等命中')
  await expect(page.getByTestId('head-version')).toHaveText('MAP-REGION-0003')

  // ---- 2. 新层级刷新：成员级差异合并，落 0004 ----
  await page.getByTestId('refresh-members').fill(V4_MEMBERS)
  await page.getByTestId('submit-refresh').click()
  await expect(page.getByTestId('notice')).toContainText('MAP-REGION-0004')
  await expect(page.getByTestId('head-version')).toHaveText('MAP-REGION-0004')
  await page.getByTestId('tab-mapping').click()
  await expect(page.locator('[data-testid^="mapping-row-"]')).toHaveCount(11)
  const suzhou = page.getByTestId('mapping-row-M010')
  await expect(suzhou).toContainText('华东大区')
  await expect(suzhou).toContainText('MAP-REGION-0003') // 首次写入版本保留
  const ningbo = page.getByTestId('mapping-row-M011')
  await expect(ningbo).toContainText('浙江')

  // ---- 3. 连续 5 次快速刷新同输入：只落 0 个新版本，全部幂等 ----
  await page.getByTestId('tab-refresh').click()
  await page.getByTestId('burst-refresh').click()
  await expect(page.getByTestId('notice')).toContainText('新落有效版本 0 个')
  await expect(page.getByTestId('head-version')).toHaveText('MAP-REGION-0004')

  // ---- 4. 过期前置版本并发：冲突落待核对对照文件 ----
  await page.getByTestId('refresh-expected').fill('MAP-REGION-0003')
  await page.getByTestId('submit-refresh').click()
  await expect(page.getByTestId('notice')).toContainText('冲突')
  await page.getByTestId('tab-checkpoint').click()
  await expect(page.getByTestId('pending-count')).toHaveText('1')

  // ---- 5. 对照文件核对：冲突文件被驳回 ----
  await page.getByTestId('tab-checkpoint').click()
  const pendingRow = page.locator('[data-state="待核对"]').first()
  await expect(pendingRow).toBeVisible()
  await pendingRow.getByRole('button', { name: '核对主版本边界' }).click()
  await expect(page.getByTestId('notice')).toContainText('核对未通过')
  await expect(page.locator('[data-state="已驳回"]').first()).toBeVisible()

  // 打开对照文件：来源输入可追溯
  await page.locator('[data-state="已驳回"]').first().getByRole('button', { name: '打开' }).click()
  await expect(page.getByTestId('cp-modal')).toBeVisible()
  await expect(page.getByTestId('cp-modal')).toContainText('来源输入')
  await page.getByRole('button', { name: '关闭' }).click()

  // ---- 6. 分析接口消费并持久化 ----
  await page.getByTestId('tab-analyze').click()
  await page.getByTestId('run-analyze').click()
  await expect(page.getByTestId('analysis-result')).toBeVisible()
  await expect(page.getByTestId('analysis-result')).toContainText('COMMIT-MAP-REGION-0004')
  await expect(page.getByTestId('analysis-result')).toContainText('维度分析接口')

  // ---- 7. 回退到上一历史版本（0003）：苏州归属恢复为江苏 ----
  await page.getByTestId('rollback-btn').click()
  await expect(page.getByTestId('notice')).toContainText('已回退至 MAP-REGION-0003')
  await page.getByTestId('tab-mapping').click()
  await expect(page.getByTestId('head-version')).toHaveText('MAP-REGION-0005')
  await expect(page.locator('[data-testid^="mapping-row-"]')).toHaveCount(11) // 10 生效 + 1 停用
  const suzhouAfter = page.getByTestId('mapping-row-M010')
  await expect(suzhouAfter).toContainText('江苏')

  // ---- 8. 重新进入页面：成员归属保持不变 ----
  await page.goto('/dimension_mapping')
  await expect(page.getByTestId('head-version')).toHaveText('MAP-REGION-0005')
  await expect(page.getByTestId('mapping-row-M010')).toContainText('江苏')
  await expect(page.getByTestId('mapping-row-M011')).toContainText('已停用')

  // ---- 9. 导出：映射表与对照文件 ----
  const mappingDownload = page.waitForEvent('download')
  await page.getByTestId('export-mapping').click()
  const mappingFile = await mappingDownload
  expect(mappingFile.suggestedFilename()).toBe('dimension_member_mapping.csv')

  await page.getByTestId('tab-checkpoint').click()
  const cpDownload = page.waitForEvent('download')
  await page.locator('[data-testid^="cp-export-"]').first().click()
  const cpFile = await cpDownload
  expect(cpFile.suggestedFilename()).toMatch(/^checkpoint_CHK-.*\.csv$/)

  expect(browserErrors).toEqual([])
})
