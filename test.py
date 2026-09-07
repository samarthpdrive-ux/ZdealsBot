import asyncio
import aiohttp

from services.reseller_manager import ResellerManager


async def main():

    reseller = ResellerManager(
        base_url="https://arrsnetworkzone.in",
        api_key="AK_goIAW9WXCh3vIaP6Ox7bLRpGv7U5T4da",
    )

    connected, message = await reseller.test_connection()

    if not connected:
        print(f"❌ Reseller connection failed: {message}")
        return

    print(f"✅ {message}")

    balance = await reseller.get_balance()

    print("\n💰 BALANCE")
    print(f"${balance:.2f}")

    products = await reseller.get_products()

    print(f"\n📦 PRODUCTS FOUND: {len(products)}")

    for product in products:
        print(
            f"- {product.get('service_id')} | "
            f"{product.get('name')} | "
            f"${product.get('price')} | "
            f"Stock: {product.get('stock')}"
        )


if __name__ == "__main__":
    asyncio.run(main())