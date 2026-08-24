const { chromium } = require('playwright-extra');
const stealth = require('puppeteer-extra-plugin-stealth')();
chromium.use(stealth);

(async () => {
  // 读取环境变量中的代理设置
  const proxyConfig = process.env.PROXY_SERVER ? {
    server: process.env.PROXY_SERVER,
    username: process.env.PROXY_USERNAME || '',
    password: process.env.PROXY_PASSWORD || ''
  } : undefined;

  console.log(proxyConfig ? 'Using residential proxy...' : 'No proxy provided, running directly...');

  const browser = await chromium.launch({
    headless: true,
    proxy: proxyConfig,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-blink-features=AutomationControlled'
    ]
  });

  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    viewport: { width: 1280, height: 720 },
    locale: 'en-US'
  });

  const page = await context.newPage();

  try {
    console.log('Navigating to target...');
    await page.goto('https://dashboard.digitalplat.org/', { 
      waitUntil: 'domcontentloaded', 
      timeout: 60000 
    });

    console.log('Waiting for Cloudflare verification to process...');
    
    // 循环检测并尝试点击 Cloudflare 复选框（如果存在）
    for (let i = 0; i < 10; i++) {
      await page.waitForTimeout(2000);
      for (const frame of page.frames()) {
        if (frame.url().includes('cloudflare') || frame.url().includes('turnstile')) {
          const checkbox = await frame.$('input[type="checkbox"], .mark');
          if (checkbox) {
            console.log('Found Cloudflare checkbox, clicking...');
            await checkbox.click();
          }
        }
      }
    }

    // 额外留出渲染时间
    await page.waitForTimeout(3000);

    await page.screenshot({ path: 'screenshot.png', fullPage: true });
    console.log('Screenshot saved successfully!');
  } catch (err) {
    console.error('Execution encountered an error:', err);
    await page.screenshot({ path: 'screenshot.png' });
  } finally {
    await browser.close();
  }
})();
