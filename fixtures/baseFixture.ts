
import { test as base } from '@playwright/test'
import { HomePage } from '../pages/HomePage'
import { LoginPage } from '../pages/LoginPage'
import { SearchPage } from '../pages/SearchPage'
import { CruiseDetailsPage } from '../pages/CruiseDetailsPage'
import { CabinPage } from '../pages/CabinPage'
import { CartPage } from '../pages/CartPage'
import { consumeHealingReportLogs } from '../wraper-healer/healingReportLogger'

type Fixtures = {
  homePage: HomePage
  loginPage: LoginPage
  searchPage: SearchPage
  cruiseDetailsPage: CruiseDetailsPage
  cabinPage: CabinPage
  cartPage: CartPage
  healingReport: void
}

export const test = base.extend<Fixtures>({

  homePage: async ({ page }, use) => {
    await use(new HomePage(page))
  },

  loginPage: async ({ page }, use) => {
    await use(new LoginPage(page))
  },

  searchPage: async ({ page }, use) => {
    await use(new SearchPage(page))
  },

  cruiseDetailsPage: async ({ page }, use) => {
    await use(new CruiseDetailsPage(page))
  },

  cabinPage: async ({ page }, use) => {
    await use(new CabinPage(page))
  },

  cartPage: async ({ page }, use) => {
    await use(new CartPage(page))
  },
  healingReport: [async ({ }, use, testInfo) => {
    await use()
    const logs = consumeHealingReportLogs(testInfo)
    if (!logs.length) return
    await testInfo.attach('healing-log', {
      body: Buffer.from(logs.join('\n'), 'utf-8'),
      contentType: 'text/plain'
    })
  }, { auto: true }]

})

export const expect = test.expect
