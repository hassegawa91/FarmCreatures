using UnityEngine;

namespace FarmCreatures.Creatures.Visual
{
    public class CreatureIdlePulse : MonoBehaviour
    {
        [SerializeField] private float pulseSpeed = 2f;
        [SerializeField] private float pulseAmount = 0.06f;

        private Vector3 originalScale;

        private void Awake()
        {
            originalScale = transform.localScale;
        }

        private void Update()
        {
            float pulse = Mathf.Sin(Time.time * pulseSpeed) * pulseAmount;
            transform.localScale = originalScale + Vector3.one * pulse;
        }
    }
}
