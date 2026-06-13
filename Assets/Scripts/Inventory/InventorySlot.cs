using System;

namespace FarmCreatures.Inventory
{
    [Serializable]
    public class InventorySlot
    {
        public ItemData item;
        public int amount;

        public bool IsEmpty => item == null || amount <= 0;

        public InventorySlot(ItemData item, int amount)
        {
            this.item = item;
            this.amount = amount;
        }

        public void Clear()
        {
            item = null;
            amount = 0;
        }
    }
}
