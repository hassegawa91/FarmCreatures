using UnityEngine;

namespace FarmCreatures.Creatures.Care
{
    public class CreatureCare : MonoBehaviour
    {
        [Header("Care")]
        [SerializeField] private int care = 50;
        [SerializeField] private int maxCare = 100;

        public int Care => care;
        public int MaxCare => maxCare;

        public void AddCare(int amount)
        {
            care = Mathf.Clamp(care + amount, 0, maxCare);
            Debug.Log($"{name} cuidado: {care}/{maxCare}");
        }

        public bool HasCare(int minimum)
        {
            return care >= minimum;
        }
    }
}
