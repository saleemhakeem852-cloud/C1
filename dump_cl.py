import sys
import time
from craigslist import make_driver

def main():
    print("Starting driver...")
    driver = make_driver(headless=True)
    try:
        url = "https://post.craigslist.org/c/sss"
        print(f"Navigating to {url}...")
        driver.get(url)
        time.sleep(5)
        print("Page title:", driver.title)
        
        # Dump HTML
        html = driver.page_source
        with open("cl_page_source.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("Saved page source to cl_page_source.html")
        
        # Let's inspect form inputs/labels/uls
        inputs = driver.find_elements("tag name", "input")
        print(f"Found {len(inputs)} inputs:")
        for idx, inp in enumerate(inputs):
            print(f"Input {idx}: type={inp.get_attribute('type')}, name={inp.get_attribute('name')}, value={inp.get_attribute('value')}, id={inp.get_attribute('id')}")
            
        labels = driver.find_elements("tag name", "label")
        print(f"Found {len(labels)} labels:")
        for idx, lbl in enumerate(labels[:30]):
            print(f"Label {idx}: text='{lbl.text}', for='{lbl.get_attribute('for')}'")
            
        uls = driver.find_elements("tag name", "ul")
        print(f"Found {len(uls)} uls")
        for idx, ul in enumerate(uls):
            lis = ul.find_elements("tag name", "li")
            print(f"UL {idx} has {len(lis)} LIs")
            for li_idx, li in enumerate(lis):
                print(f"  LI {li_idx}: text='{li.text.strip()}'")
                
    except Exception as e:
        print("Error:", e)
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
