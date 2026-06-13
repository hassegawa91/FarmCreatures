using UnityEngine;

namespace FarmCreatures.Inventory
{
    public enum ItemType
    {
        Resource,
        Seed,
        Food,
        Tool,
        CreatureItem,
        BuildingMaterial
    }

    [CreateAssetMenu(fileName = "NewItemData", menuName = "FarmCreatures/Inventory/Item Data")]
    public class ItemData : ScriptableObject
    {
        public string itemId;
        public string displayName;
        public ItemType itemType;
        public Sprite icon;
        public int maxStack = 99;
        [TextArea] public string description;
    }
}
