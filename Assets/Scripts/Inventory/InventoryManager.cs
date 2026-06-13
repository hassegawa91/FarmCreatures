using System.Collections.Generic;
using UnityEngine;

namespace FarmCreatures.Inventory
{
    public class InventoryManager : MonoBehaviour
    {
        public static InventoryManager Instance { get; private set; }

        [SerializeField] private int maxSlots = 24;
        [SerializeField] private List<InventorySlot> slots = new List<InventorySlot>();

        public IReadOnlyList<InventorySlot> Slots => slots;

        private void Awake()
        {
            if (Instance != null && Instance != this)
            {
                Destroy(gameObject);
                return;
            }

            Instance = this;
            DontDestroyOnLoad(gameObject);
            InitializeSlots();
        }

        private void InitializeSlots()
        {
            while (slots.Count < maxSlots)
                slots.Add(new InventorySlot(null, 0));
        }

        public bool AddItem(ItemData item, int amount)
        {
            if (item == null || amount <= 0)
                return false;

            foreach (InventorySlot slot in slots)
            {
                if (slot.item == item && slot.amount < item.maxStack)
                {
                    int available = item.maxStack - slot.amount;
                    int toAdd = Mathf.Min(available, amount);
                    slot.amount += toAdd;
                    amount -= toAdd;

                    if (amount <= 0)
                        return true;
                }
            }

            foreach (InventorySlot slot in slots)
            {
                if (slot.IsEmpty)
                {
                    int toAdd = Mathf.Min(item.maxStack, amount);
                    slot.item = item;
                    slot.amount = toAdd;
                    amount -= toAdd;

                    if (amount <= 0)
                        return true;
                }
            }

            return false;
        }

        public bool HasItem(ItemData item, int amount)
        {
            if (item == null || amount <= 0)
                return false;

            int total = 0;

            foreach (InventorySlot slot in slots)
            {
                if (slot.item == item)
                    total += slot.amount;
            }

            return total >= amount;
        }

        public bool RemoveItem(ItemData item, int amount)
        {
            if (!HasItem(item, amount))
                return false;

            int remaining = amount;

            foreach (InventorySlot slot in slots)
            {
                if (slot.item != item)
                    continue;

                int toRemove = Mathf.Min(slot.amount, remaining);
                slot.amount -= toRemove;
                remaining -= toRemove;

                if (slot.amount <= 0)
                    slot.Clear();

                if (remaining <= 0)
                    return true;
            }

            return true;
        }

        public InventorySlot FindFirst(ItemData item)
        {
            foreach (InventorySlot slot in slots)
            {
                if (slot.item == item && slot.amount > 0)
                    return slot;
            }

            return null;
        }
    }
}
